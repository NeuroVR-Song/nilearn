"""
Implementation of algorithm for sparse multi-task learning of gaussian
graphical models.
"""
# Authors: Philippe Gervais
# License: simplified BSD

import warnings
import collections
import operator

import numpy as np
import scipy
import scipy.optimize

import sklearn.cross_validation
import sklearn.covariance
from sklearn.utils.extmath import fast_logdet
from sklearn.covariance import empirical_covariance
from sklearn.base import BaseEstimator

from sklearn.externals.joblib import Memory, delayed, Parallel

from ._utils import CacheMixin, LogMixin
from .testing import is_spd


def symmetrize(M):
    M[...] = M + M.T
    M[...] /= 2.


def rho_max(emp_covs, n_samples):
    """
    Parameters
    ----------
    emp_covs: numpy.ndarray
        covariance matrix for each task.
        shape (variable number, variable number, covariance matrix number)

    n_samples: array-like
        number of samples used in the computation of every covariance matrix.

    Returns
    -------
    rho_max: Minimal value for regularization parameter that gives a
        full-sparse matrix.
    """
    A = np.copy(emp_covs)

    for k in range(emp_covs.shape[-1]):
        # Set diagonal to zero
        A[..., k].flat[::A.shape[0] + 1] = 0
        A[..., k] *= n_samples[k]

    return np.max(np.sqrt((A ** 2).sum(axis=-1)))


def _group_sparse_covariance_costs(n_tasks, n_var, n_samples, rho, omega,
                                   emp_covs, display=False, debug=False):
    """Compute group sparse covariance costs during computation.

    Returns
    -------
    primal_cost: float
        value of primal cost at current point. This value is minimized by the
        algorithm.
    gap: float
        value of duality gap at current point, with a feasible dual point. This
        value is supposed to always be negative, and vanishing for the optimal
        point.
    """
    # Signs for primal and dual costs are inverted compared to the H&S paper,
    # to match scikit-learn's usage of *minimizing* the primal problem.

    ## Primal cost
    ll = 0  # log-likelihood
    sps = 0  # scalar products
    for k in xrange(n_tasks):
        t = fast_logdet(omega[..., k])
        sp = (omega[..., k] * emp_covs[..., k]).sum()
        ll += n_samples[k] * (t - sp)
        sps += n_samples[k] * sp

    # L(1,2)-norm
    l2 = np.sqrt((omega ** 2).sum(axis=-1))
    l12 = l2.sum() - np.diag(l2).sum()  # Do not count diagonal terms
    cost = - (ll - rho * l12)

    ## Dual cost: rather heavy computation.
    # Compute A(k)
    A = np.empty(omega.shape, dtype=omega.dtype)  # TODO: allocate once
    for k in xrange(n_tasks):
        omega_inv = np.linalg.inv(omega[..., k])
        assert is_spd(omega_inv)
        A[..., k] = n_samples[k] * (omega_inv - emp_covs[..., k])
        if debug:
            np.testing.assert_almost_equal(A[..., k], A[..., k].T)

    # Project A on the set of feasible points
    rho_max = np.sqrt((A ** 2).sum(axis=-1))
    mask = rho_max > rho
    for k in range(A.shape[-1]):
        A[mask, k] *= rho / rho_max[mask]
        A[..., k].flat[::A.shape[0] + 1] = 0  # essential

    rho_max = np.sqrt((A ** 2).sum(axis=-1)).max()
    dual_cost = 0
    for k in xrange(n_tasks):
        B = emp_covs[..., k] + A[..., k] / n_samples[k]
        if debug:
            assert is_spd(B)
        dual_cost += n_samples[k] * (n_var + fast_logdet(B))
    gap = cost - dual_cost

    if display:
        print("primal cost / duality gap: {cost: .8f} / {gap:.8f}".format(
            gap=gap, cost=cost))

    return (cost, gap)


# The signatures of quad_trust_region and quad_trust_region_deriv are
# complicated, but this allows for some interesting optimizations.
def quad_trust_region(alpha, q, two_ccq, cc, rho2):
    """This value is optimized to zero by the Newton-Raphson step."""
    return rho2 - (cc / ((1. + alpha * q) ** 2)).sum()


def quad_trust_region_deriv(alpha, q, two_ccq, cc, rho2):
    """Derivative of quad_trust_region."""
    return (two_ccq / (1. + alpha * q) ** 3).sum()


def update_vectors(full, n):
    """full is a (N, N) matrix.

    This function is a helper function for updating the submatrix equals to
    "full" with row n + 1 and column n + 1 removed. The initial state of the
    submatrix is supposed to be "full" with row and column n removed.

    This functions returns the new value of row and column n in the submatrix.
    Thus, if h, v are the return values of this function, the submatrix must
    be updated this way: sub[n, :] = h ; sub[:, n] = v
    """
    v = np.ndarray((full.shape[0] - 1,), dtype=full.dtype)
    v[:n + 1] = full[:n + 1, n]
    v[n + 1:] = full[n + 2:, n]

    h = np.ndarray((full.shape[1] - 1,), dtype=full.dtype)
    h[:n + 1] = full[n, :n + 1]
    h[n + 1:] = full[n, n + 2:]

    return h, v


def update_submatrix(full, sub, sub_inv, p):
    """Update submatrix and its inverse.

    sub_inv is the inverse of the submatrix of "full" obtained by removing
    the p-th row and column.

    sub_inv is modified in-place. After execution of this function, it contains
    the inverse of the submatrix of "full" obtained by removing the n+1-th row
    and column.

    This computation is based on Sherman-Woodbury-Morrison identity.
    """

    n = p - 1
    h, v = update_vectors(full, n)

    # change row
    coln = sub_inv[:, n]
    V = h - sub[n, :]
    coln = coln / (1. + np.dot(V, coln))
    sub_inv -= np.outer(coln, np.dot(V, sub_inv))
    sub[n, :] = h

    # change column
    rown = sub_inv[n, :]
    U = v - sub[:, n]
    rown = rown / (1. + np.dot(rown, U))
    sub_inv -= np.outer(np.dot(sub_inv, U), rown)
    sub[:, n] = v   # equivalent to sub[n, :] += U


def assert_submatrix(full, sub, n):
    """Check that "sub" is the matrix obtained by removing the p-th col and row
    in "full". Used only for debugging.
    """
    true_sub = np.ndarray(shape=sub.shape, dtype=sub.dtype)
    true_sub[:n, :n] = full[:n, :n]
    true_sub[n:, n:] = full[n + 1:, n + 1:]
    true_sub[:n, n:] = full[:n, n + 1:]
    true_sub[n:, :n] = full[n + 1:, :n]

    np.testing.assert_almost_equal(true_sub, sub)


def group_sparse_covariance(tasks, rho, max_iter=10, tol=1e-4,
                            assume_centered=False, verbose=0, dtype=np.float64,
                            return_costs=False, debug=False,
                            precisions_init=None):
    """Compute sparse precision matrices and covariance matrices.

    The precision matrices returned by this function are sparse, and share a
    common sparsity pattern: all have zeros at the same location. This is
    achieved by simultaneous computation of all precision matrices at the
    same time.

    Running time is linear on max_iter, and number of tasks (len(tasks)), but
    cubic on number of signals (tasks[0].shape[1]).

    Parameters
    ==========
    tasks: list of numpy.ndarray
        input tasks. Each task is a 2D array, whose columns contain signals.
        Each array shape must be (sample number, feature number). The sample
        number can vary from task to task, but all tasks must have the same
        number of features (i.e. of columns).

    rho: float
        regularization parameter. With normalized covariances matrices and
        number of samples, sensible values lie in the [0, 1] range(zero is
        no regularization: output is not sparse)

    tol: positive float or None, optional
        The tolerance to declare convergence: if the dual gap goes below
        this value, iterations are stopped. If None, no check is performed.

    max_iter: int, optional
        maximum number of iterations. The default value (10) is rather
        conservative.

    assume_centered: bool, optional
        if True, assume that all input signals are centered. This slightly
        decreases computation time by avoiding useless computation.

    verbose: int, optional
        verbosity level. Zero means "no message".

    dtype: numpy dtype, optional
        type of returned matrices. Defaults to 8-byte floats (double).

    return_costs: bool, optional
        if True, return the value taken by the objective and the duality gap
        functions for each iteration in addition to the matrices.
        Default: False.

    debug: bool, optional
        if True, perform checks during computation. It can help find
        numerical problems, but increases computation time a lot.

    Returns
    =======
    emp_covs: numpy.ndarray
        empirical covariance matrices (output of
        sklearn.covariance.empirical_covariance)

    precision: numpy.ndarray
        estimated precision matrices

    costs : list of (objective, duality_gap) pairs
        The list of values of the objective function and the duality gap at
        each iteration. Returned only if return_costs is True

    Notes
    =====
    The present algorithm is based on:

    Jean Honorio and Dimitris Samaras.
    "Simultaneous and Group-Sparse Multi-Task Learning of Gaussian Graphical
    Models". arXiv:1207.4255 (17 July 2012). http://arxiv.org/abs/1207.4255.
    """
    if not isinstance(rho, (int, float)) or rho < 0:
        raise ValueError("Regularization parameter rho must be a "
                         "positive number.\n"
                         "You provided: {0}".format(str(rho)))
    emp_covs, n_samples, n_tasks, n_var = \
              empirical_covariances(tasks, assume_centered=assume_centered,
                                    dtype=dtype, debug=debug)
    del tasks  # reduces memory usage in some cases.

    if precisions_init is None:
        omega = np.ndarray(shape=emp_covs.shape, dtype=emp_covs.dtype)
        for k in xrange(n_tasks):
            # Values on main diagonals should be far from zero, because they
            # are timeseries energy.
            omega[..., k] = np.diag(1. / np.diag(emp_covs[..., k]))
    else:
        print("restart")
        omega = precisions_init.copy()

    # Preallocate arrays
    y = np.ndarray(shape=(n_tasks, n_var - 1), dtype=emp_covs.dtype)
    u = np.ndarray(shape=(n_tasks, n_var - 1), dtype=emp_covs.dtype)
    y_1 = np.ndarray(shape=(n_tasks, n_var - 2), dtype=emp_covs.dtype)
    h_12 = np.ndarray(shape=(n_tasks, n_var - 2), dtype=emp_covs.dtype)
    q = np.ndarray(shape=(n_tasks,), dtype=emp_covs.dtype)
    c = np.ndarray(shape=(n_tasks,), dtype=emp_covs.dtype)
    W = np.ndarray(shape=(omega.shape[0] - 1, omega.shape[1] - 1,
                          omega.shape[2]),
                   dtype=emp_covs.dtype)
    Winv = np.ndarray(shape=W.shape, dtype=emp_covs.dtype)

    # Optional.
    costs = []

    # Start optimization loop. Variables are named following (mostly) the
    # Honorio-Samaras paper notations.
    for n in xrange(max_iter):
        if verbose >= 1:
            print("* iteration {iter_n:d} ({percentage:.0f} %) ...".format(
                iter_n=n, percentage=100. * n / max_iter))

        for p in xrange(n_var):

            if p == 0:
                # Initial state: remove first col/row
                W = omega[1:, 1:, :].copy()   # stack of W(k)
                Winv = np.ndarray(shape=W.shape, dtype=np.float)
                for k in xrange(W.shape[2]):
                    # stack of W^-1(k)
                    Winv[..., k] = np.linalg.inv(W[..., k])
                    if debug:
                        np.testing.assert_almost_equal(
                            np.dot(Winv[..., k], W[..., k]),
                            np.eye(Winv[..., k].shape[0]), decimal=12)
                        assert_submatrix(omega[..., k], W[..., k], p)
            else:
                # Update W and Winv
                if debug:
                    omega_orig = omega.copy()

                for k in xrange(n_tasks):
                    update_submatrix(omega[..., k],
                                     W[..., k], Winv[..., k], p)
                    if debug:
                        assert_submatrix(omega[..., k], W[..., k], p)
                        np.testing.assert_almost_equal(
                            np.dot(Winv[..., k], W[..., k]),
                            np.eye(Winv[..., k].shape[0]), decimal=12)
                        assert(is_spd(W[..., k]))
                        assert(is_spd(Winv[..., k], decimal=14))
                if debug:
                    np.testing.assert_almost_equal(omega_orig, omega)

            # In the following lines, implicit loop on k (tasks)
            # Extract y and u
            y[:, :p] = omega[:p, p, :].T
            y[:, p:] = omega[p + 1:, p, :].T

            u[:, :p] = emp_covs[:p, p, :].T
            u[:, p:] = emp_covs[p + 1:, p, :].T

            for m in xrange(n_var - 1):
                # Coordinate descent on y

                # T(k) -> n_samples[k]
                # v(k) -> emp_covs[p, p, k]
                # h_22(k) -> Winv[m, m, k]
                # h_12(k) -> Winv[:m, m, k],  Winv[m+1:, m, k]
                # y_1(k) -> y[k, :m], y[k, m+1:]
                # u_2(k) -> u[k, m]
                h_12[:, :m] = Winv[:m, m, :].T
                h_12[:, m:] = Winv[m + 1:, m, :].T
                y_1[:, :m] = y[:, :m]
                y_1[:, m:] = y[:, m + 1:]

                c[:] = - n_samples * (
                    emp_covs[p, p, :] * (h_12 * y_1).sum(axis=1) + u[:, m]
                    )
                c2 = np.sqrt(np.dot(c, c))

                # x -> y[:][m]
                if c2 <= rho:
                    y[:, m] = 0  # x* = 0
                else:
                    # q(k) -> T(k) * v(k) * h_22(k)
                    # \lambda -> alpha   (lambda is a Python keyword)
                    q = n_samples * emp_covs[p, p, :] * Winv[m, m, :]
                    if debug:
                        assert(np.all(q > 0))
                    # x* = \lambda* diag(1 + \lambda q)^{-1} c
                    # Precompute some quantities
                    cc = c * c
                    two_ccq = 2. * cc * q
                    # tolerance does not seem to be important for
                    # numerical stability (tol=1e-2 works) but has an
                    # effect on final duality gap value.
                    alpha = scipy.optimize.newton(
                        quad_trust_region, 0,
                        fprime=quad_trust_region_deriv,
                        args=(q, two_ccq, cc, rho ** 2),
                        maxiter=50, tol=1.5e-6)

                    remainder = quad_trust_region(
                        alpha, q, two_ccq, cc, rho ** 2)

                    if abs(remainder) > 0.1:
                        warnings.warn("Newton-Raphson step did not converge.\n"
                                      "This indicates a badly conditioned "
                                      "system.")

                    if debug:
                        assert alpha >= 0, alpha
                    y[:, m] = (alpha * c) / (1. + alpha * q)  # x*

            # Copy back y in omega (column and row)
            omega[:p, p, :] = y[:, :p].T
            omega[p + 1:, p, :] = y[:, p:].T
            omega[p, :p, :] = y[:, :p].T
            omega[p, p + 1:, :] = y[:, p:].T

            for k in xrange(n_tasks):
                omega[p, p, k] = 1. / emp_covs[p, p, k] + np.dot(
                    np.dot(y[k, :], Winv[..., k]), y[k, :])

                if debug:
                    assert(is_spd(omega[..., k]))

        if return_costs or tol is not None:
            objective, duality_gap = _group_sparse_covariance_costs(
                n_tasks, n_var, n_samples, rho, omega, emp_covs,
                display=verbose >= 2, debug=debug)

        if return_costs:
            costs.append((objective, duality_gap))

        if tol is not None and duality_gap < tol:
            break

    if return_costs:
        return emp_covs, omega, costs
    else:
        return emp_covs, omega


class GroupSparseCovariance(BaseEstimator, CacheMixin, LogMixin):
    """Covariance and precision matrix estimator.

    The algorithm used is based on what is described in:

    Jean Honorio and Dimitris Samaras.
    "Simultaneous and Group-Sparse Multi-Task Learning of Gaussian Graphical
    Models". arXiv:1207.4255 (17 July 2012). http://arxiv.org/abs/1207.4255.

    Parameters
    ----------
    rho: float
        regularization parameter. With normalized covariances matrices and
        number of samples, sensible values lie in the [0, 1] range(zero is
        no regularization: output is not sparse)

    tol: positive float, optional
        The tolerance to declare convergence: if the dual gap goes below
        this value, iterations are stopped

    max_iter: int
        maximum number of iterations. The default value (10) is rather
        conservative.

    verbose: int
        verbosity level. Zero means "no message".

    assume_centered: bool
        if True, assume that all signals passed to fit() are centered.

    return_costs: bool
        if True, objective and duality gap are computed for each iteration and
        returned as self.objective_ and self.duality_gap_ respectively.

    memory: instance of joblib.Memory or string
        Used to cache the masking process.
        By default, no caching is done. If a string is given, it is the
        path to the caching directory.

    memory_level: int, optional
        Caching aggressiveness. Higher values mean more caching.

    Attributes
    ----------
    `covariances_`: 3D numpy.ndarray
        maximum likelihood covariance estimations.
        Shape: (n_features, n_features, n_tasks)

    `precisions_`: 3D numpy.ndarray
        precisions matrices estimated using Antonio & Samaras algorithm.
        Shape: (n_features, n_features, n_tasks)
    """

    def __init__(self, rho=0.1, tol=1e-4, max_iter=10, verbose=1,
                 assume_centered=False, return_costs=False,
                 memory=Memory(cachedir=None), memory_level=0):
        self.rho = rho
        self.tol = tol
        self.max_iter = max_iter
        self.assume_centered = assume_centered
        self.return_costs = return_costs

        self.memory = memory
        self.memory_level = memory_level
        self.verbose = verbose

    def fit(self, tasks, y=None):
        """Fits the group sparse precision model according to the given
        training data and parameters.

        Parameters
        ----------
        tasks: list of numpy.ndarray
            input tasks. Each task is a 2D array, whose columns contain
            signals. Each array shape must be (sample number, feature number).
            The sample number can vary from task to task, but all tasks must
            have the same number of features (i.e. of columns).

        Attributes
        ----------
        `covariances_`: numpy.ndarray
            empirical covariances

        `precisions_`: numpy.ndarray
            precision matrices

        `objective_`, `duality_gap`: list of floats
            optimized value for each iteration (primal problem) and duality gap
            values. These attribute are computed only if return_costs is True.

        Returns
        -------
        self: object
            the object itself. Useful for chaining operations.
        """

        self.log("Computing precision matrices")
        ret = self._cache(
            group_sparse_covariance, memory_level=1)(
                tasks, self.rho, tol=self.tol, max_iter=self.max_iter,
                assume_centered=self.assume_centered,
                verbose=self.verbose - 1, debug=False,
                return_costs=self.return_costs, dtype=np.float64)

        if self.return_costs:
            self.covariances_, self.precisions_, costs = ret
            self.objective_, self.duality_gap_ = zip(*costs)
        else:
            self.covariances_, self.precisions_ = ret

        return self


def empirical_covariances(tasks, assume_centered=False, dtype=np.float64,
                          debug=False):
    """Compute empirical covariances for several signals.

    Returns
    -------
    emp_covs
    n_samples (normalized)
    n_tasks
    n_var
    """
    if not hasattr(tasks, "__iter__"):
        raise ValueError("'tasks' input argument must be an iterable. "
                         "You provided {0}".format(tasks.__class__))

    n_tasks = [s.shape[1] for s in tasks]
    if len(set(n_tasks)) > 1:
        raise ValueError("All tasks must have the same number of features.\n"
                         "You provided: {0}".format(str(n_tasks)))
    n_tasks = len(tasks)
    n_var = tasks[0].shape[1]

    emp_covs = np.empty((n_var, n_var, n_tasks), dtype=dtype)
    for k, s in enumerate(tasks):
        emp_covs[..., k] = empirical_covariance(
            s, assume_centered=assume_centered)
        symmetrize(emp_covs[..., k])
        if debug:
            assert(is_spd(emp_covs[..., k]))

    n_samples = np.asarray([s.shape[0] for s in tasks], dtype=np.float64)
    n_samples /= n_samples.sum()

    return emp_covs, n_samples, n_tasks, n_var


def group_sparse_covariance_path(train_tasks, test_tasks, rhos, max_iter=10,
                                 assume_centered=False, verbose=0,
                                 dtype=np.float64, debug=False):

    # FIXME: Unoptimized version. Can do much better (see group_lasso_)
    test_covs, _, _, _ = empirical_covariances(
        test_tasks, assume_centered=assume_centered, dtype=dtype, debug=debug)
    n_samples = np.asarray([task.shape[0] for task in train_tasks],
                           dtype=np.float64)
    n_samples /= n_samples.sum()

    scores = []
    precisions_init = None
    for rho in reversed(rhos):
        _, precisions = group_sparse_covariance(
            train_tasks, rho, max_iter=max_iter,
            assume_centered=assume_centered,
            verbose=verbose, return_costs=False, dtype=dtype, debug=debug,
            precisions_init=precisions_init)

        # Compute score for current rho value
        task_score = []
        for k in range(precisions.shape[2]):
            task_score.append(n_samples[k] * sklearn.covariance.log_likelihood(
                test_covs[..., k], precisions[..., k]))
        scores.append(sum(task_score))
        precisions_init = precisions

    return [s for s in reversed(scores)]


class GroupSparseCovarianceCV(object):
    # See also GraphLasso in scikit-learn.
    """
    Parameters
    ----------
    cv: integer
        number of folds in a K-fold cross-validation scheme.
    """
    def __init__(self, rhos=4, n_refinements=4, cv=None, max_iter=10,
                 assume_centered=False, verbose=1,
                 memory=Memory(cachedir=None), memory_level=0,
                 n_jobs=1, debug=False, dtype=np.float64):
        self.rhos = rhos
        self.n_refinements = n_refinements
        self.cv = cv
        self.max_iter = max_iter
        self.assume_centered = assume_centered
        self.dtype = dtype

        self.verbose = verbose
        self.memory = memory
        self.memory_level = memory_level
        self.n_jobs = n_jobs
        self.debug = debug

    def fit(self, tasks, y=None):
        """Compute cross-validated group-sparse precision.

        Parameters
        ----------
        tasks: list of numpy.ndarray
            input tasks. Each task is a 2D array, whose columns contain
            signals. Each array shape must be (sample number, feature number).
            The sample number can vary from task to task, but all tasks must
            have the same number of features (i.e. of columns).

        Attributes
        ----------
        `covariances_`: numpy.ndarray
        `precision_`: numpy.ndarray
        `rho_`: selected value for penalization parameter
        `cv_rhos`: list of float
            All penalization values explored.
        `cv_scores`: numpy.ndarray with shape (n_rhos, n_folds)
        """

        # Empirical covariances
        emp_covs, n_samples, n_tasks, n_var = \
                  empirical_covariances(tasks,
                                        assume_centered=self.assume_centered,
                                        dtype=self.dtype, debug=self.debug)

        # One cv generator per task must be created, because each task can
        # have a different number of samples.
        cv = []
        for k in range(n_tasks):
            cv.append(sklearn.cross_validation.check_cv(
                self.cv, tasks[k], None, classifier=False))

        # List of (rho, scores, covs)
        path = list()
        n_rhos = self.rhos

        if isinstance(n_rhos, collections.Sequence):
            rhos = list(self.rhos)
            n_rhos = len(rhos)
            n_refinements = 1
        else:
            n_refinements = self.n_refinements
            rho_1 = rho_max(emp_covs, n_samples)
            rho_0 = 1e-2 * rho_1
            rhos = np.logspace(np.log10(rho_0), np.log10(rho_1),
                                 n_rhos)[::-1]

#        covs_init = (None, None, None)
        for i in range(n_refinements):
            # Compute the cross-validated loss on the current grid
            train_test_tasks = []
            for train_test in zip(*cv):
                assert(len(train_test) == n_tasks)
                train_test_tasks.append(zip(*[(task[train, :], task[test, :])
                                              for task, (train, test)
                                              in zip(tasks, train_test)]))

            this_path = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
                delayed(group_sparse_covariance_path)(
                    train_tasks, test_tasks, rhos, max_iter=self.max_iter,
                    assume_centered=self.assume_centered, verbose=self.verbose,
                    dtype=self.dtype, debug=self.debug)
                for train_tasks, test_tasks in train_test_tasks)

            # this_path[i] is the scores obtained with the i-th folding,
            # for varying rho.
            scores = [np.mean(sc) for sc in zip(*this_path)]
            # scores is the mean score obtained for a given value of rho.
            path.extend(zip(rhos, scores))
            path = sorted(path, key=operator.itemgetter(0), reverse=True)

            # Find the maximum (avoid using the built-in 'max' function to
            # have a fully-reproducible selection of the smallest rho
            # in case of equality)
            best_score = -np.inf
            last_finite_idx = 0
            for index, (rho, this_score) in enumerate(path):
                if this_score >= .1 / np.finfo(np.float).eps:
                    this_score = np.nan
                if np.isfinite(this_score):
                    last_finite_idx = index
                if this_score >= best_score:
                    best_score = this_score
                    best_index = index

            # Refine the grid
            if best_index == 0:
                # We do not need to go back: we have chosen
                # the highest value of rho for which there are
                # non-zero coefficients
                rho_1 = path[0][0]
                rho_0 = path[1][0]
#                covs_init = path[0][-1]
            elif (best_index == last_finite_idx
                    and not best_index == len(path) - 1):
                # We have non-converged models on the upper bound of the
                # grid, we need to refine the grid there
                rho_1 = path[best_index][0]
                rho_0 = path[best_index + 1][0]
#                covs_init = path[best_index][-1]
            elif best_index == len(path) - 1:
                rho_1 = path[best_index][0]
                rho_0 = 0.01 * path[best_index][0]
#                covs_init = path[best_index][-1]
            else:
                rho_1 = path[best_index - 1][0]
                rho_0 = path[best_index + 1][0]
#                covs_init = path[best_index - 1][-1]
            rhos = np.logspace(np.log10(rho_1), np.log10(rho_0), len(rhos) + 2)
            rhos = rhos[1:-1]
            if self.verbose and n_refinements > 1:
                print("[GroupSparseCovarianceCV] Done refinement "
                      "% 2i out of %i" % (i + 1, n_refinements))

        path = list(zip(*path))
        cv_scores = list(path[1])
        rhos = list(path[0])

        self.cv_scores = np.array(cv_scores)
        self.rho_ = rhos[best_index]
        self.cv_rhos = rhos

        # Finally fit the model with the selected rho
        self.covariances_, self.precisions_ = group_sparse_covariance(
            tasks, self.rho_, max_iter=self.max_iter, verbose=self.verbose - 1,
            dtype=self.dtype, return_costs=False, debug=self.debug)
        return self

