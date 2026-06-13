"""Analytic Hierarchy Process (AHP) implementation - Saaty (1980/2008).

Public API
----------
ahp_weights(matrix)         -> AHPResult      (never raises on CR)
ahp_weights_strict(matrix)  -> AHPResult      (raises InconsistencyError if CR > 0.10)
AHPResult
AHPError
InconsistencyError

Algorithm
---------
Principal eigenvector via **power iteration**:

    1. Start with w_0 = column-normalised means of A.
    2. Each iteration: w_{k+1} = A @ w_k, then normalise to sum 1.
    3. Converge when ||w_{k+1} - w_k||_inf < 1e-10 (or 1 000 iterations max).

lambda_max is estimated from the Saaty definition:
    lambda_max = (1/n) * sum_i( (A @ w)[i] / w[i] )

CI  = (lambda_max - n) / (n - 1)
CR  = CI / RI[n]   where RI is the standard Saaty Random Index table.

Most-inconsistent triple
------------------------
For every ordered triple (i, j, k) the deviation from the reciprocal
transitivity identity is measured as:

    delta(i,j,k) = |a[i,k] - a[i,j] * a[j,k]|

The triple with the maximum delta is returned so the UI can highlight the
most problematic pair of judgments.

Standard Saaty RI table (n = 1 to 10)
--------------------------------------
n : 1     2     3     4     5     6     7     8     9    10
RI: 0.00  0.00  0.58  0.90  1.12  1.24  1.32  1.41  1.45  1.49
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np

# ---------------------------------------------------------------------------
# Saaty Random Index table (exact values from Saaty 1980/2008)
# ---------------------------------------------------------------------------

_RI: Final[dict[int, float]] = {
    1: 0.00,
    2: 0.00,
    3: 0.58,
    4: 0.90,
    5: 1.12,
    6: 1.24,
    7: 1.32,
    8: 1.41,
    9: 1.45,
    10: 1.49,
}

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class AHPError(ValueError):
    """Raised when the input matrix is structurally invalid for AHP."""


class InconsistencyError(ValueError):
    """Raised by ahp_weights_strict when CR > 0.10.

    Attributes
    ----------
    cr : float
        Computed consistency ratio.
    most_inconsistent : tuple[int, int, int]
        The (i, j, k) triple with the largest transitivity deviation,
        for UI highlighting.
    """

    def __init__(
        self,
        cr: float,
        most_inconsistent: tuple[int, int, int],
    ) -> None:
        self.cr = cr
        self.most_inconsistent = most_inconsistent
        super().__init__(
            f"AHP consistency ratio CR={cr:.4f} > 0.10. "
            f"Most inconsistent judgment triple: {most_inconsistent}."
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class AHPResult:
    """Output of the AHP computation.

    Attributes
    ----------
    weights : np.ndarray
        Principal eigenvector normalised to sum 1 (length n).
    lambda_max : float
        Estimated principal eigenvalue.
    ci : float
        Consistency Index = (lambda_max - n) / (n - 1).
    cr : float
        Consistency Ratio = ci / RI[n].  0 for n <= 2.
    consistent : bool
        True iff cr <= 0.10.
    most_inconsistent : tuple[int, int, int] | None
        (i, j, k) triple whose judgment a[i,k] deviates most from
        a[i,j]*a[j,k].  None for n < 3 (no triples exist).
    """

    weights: np.ndarray
    lambda_max: float
    ci: float
    cr: float
    consistent: bool
    most_inconsistent: tuple[int, int, int] | None = field(default=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_matrix(matrix: np.ndarray) -> int:
    """Validate AHP pairwise matrix; return n on success, raise AHPError otherwise."""
    if matrix.ndim != 2:
        raise AHPError(f"Matrix must be 2-D, got {matrix.ndim}-D array.")
    n, m = matrix.shape
    if n != m:
        raise AHPError(f"Matrix must be square; got shape {matrix.shape}.")
    if n < 2:
        raise AHPError(f"Matrix must be at least 2x2; got {n}x{n}.")
    if n > 10:
        raise AHPError(
            f"Matrix size {n} exceeds the Saaty RI table (max n=10). "
            "Extend _RI to support larger matrices."
        )

    # Positive entries
    if not np.all(matrix > 0):
        raise AHPError("All matrix entries must be strictly positive.")

    # Unit diagonal
    diag = np.diag(matrix)
    if not np.allclose(diag, 1.0, atol=1e-9):
        raise AHPError(f"diagonal entries must all equal 1.0; got {diag}.")

    # Reciprocal: a[j,i] ≈ 1 / a[i,j]
    product = matrix * matrix.T
    if not np.allclose(product, 1.0, atol=1e-6):
        bad = np.argwhere(np.abs(product - 1.0) > 1e-6)
        i0, j0 = int(bad[0, 0]), int(bad[0, 1])
        raise AHPError(
            f"Matrix is not reciprocal: a[{i0},{j0}]={matrix[i0, j0]:.6g}, "
            f"a[{j0},{i0}]={matrix[j0, i0]:.6g}; "
            f"product = {matrix[i0, j0] * matrix[j0, i0]:.6g} ≠ 1."
        )

    return n


def _power_iteration(matrix: np.ndarray, n: int) -> np.ndarray:
    """Estimate principal eigenvector of *matrix* via power iteration.

    Convergence criterion: ||w_{k+1} - w_k||_inf < 1e-10.
    Maximum iterations: 1 000 (sufficient for any n <= 10 with Saaty entries).

    Returns the normalised weight vector (sums to 1).
    """
    # Initialise with column-normalised geometric means (fast convergence)
    col_sums = matrix.sum(axis=0)
    w = (matrix / col_sums).mean(axis=1)
    w = w / w.sum()

    for _ in range(1_000):
        w_new = matrix @ w
        w_new = w_new / w_new.sum()
        if np.max(np.abs(w_new - w)) < 1e-10:
            return w_new
        w = w_new

    return w  # return best estimate even if not fully converged


def _lambda_max(matrix: np.ndarray, weights: np.ndarray) -> float:
    """Compute λmax = (1/n) * Σ_i [(A·w)_i / w_i]."""
    n = len(weights)
    aw = matrix @ weights
    # Guard against near-zero weights (shouldn't happen with positive matrix)
    ratios = np.where(weights > 1e-15, aw / weights, 0.0)
    return float(np.sum(ratios) / n)


def _most_inconsistent_triple(matrix: np.ndarray, n: int) -> tuple[int, int, int] | None:
    """Return (i, j, k) triple whose transitivity deviation is largest.

    Transitivity identity: a[i,k] should equal a[i,j] * a[j,k].
    Deviation: |a[i,k] - a[i,j] * a[j,k]|.

    Returns None if n < 3 (no ordered triples exist).
    """
    if n < 3:
        return None

    best_delta = -1.0
    best_triple: tuple[int, int, int] = (0, 1, 2)

    for i in range(n):
        for j in range(n):
            if j == i:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                delta = abs(matrix[i, k] - matrix[i, j] * matrix[j, k])
                if delta > best_delta:
                    best_delta = delta
                    best_triple = (i, j, k)

    return best_triple


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ahp_weights(matrix: np.ndarray) -> AHPResult:
    """Compute AHP weights and consistency metrics from a pairwise matrix.

    This function never raises on consistency — check ``result.consistent``
    or use ``ahp_weights_strict`` for hard rejection.

    Parameters
    ----------
    matrix : np.ndarray
        nxn positive reciprocal pairwise comparison matrix with unit diagonal.
        Entries follow the Saaty 1-9 scale.

    Returns
    -------
    AHPResult
        weights, lambda_max, ci, cr, consistent, most_inconsistent.

    Raises
    ------
    AHPError
        If the matrix is structurally invalid (non-square, non-positive,
        non-reciprocal, non-unit diagonal, or n > 10).
    """
    # Accept lists / nested lists for convenience
    mat = np.asarray(matrix, dtype=float)
    n = _validate_matrix(mat)

    weights = _power_iteration(mat, n)
    lmax = _lambda_max(mat, weights)

    ci = (lmax - n) / (n - 1) if n > 1 else 0.0
    ri = _RI[n]
    cr = (ci / ri) if ri > 0.0 else 0.0

    consistent = cr <= 0.10
    most_inc = _most_inconsistent_triple(mat, n)

    return AHPResult(
        weights=weights,
        lambda_max=lmax,
        ci=ci,
        cr=cr,
        consistent=consistent,
        most_inconsistent=most_inc,
    )


def ahp_weights_strict(matrix: np.ndarray) -> AHPResult:
    """Like ``ahp_weights`` but raises ``InconsistencyError`` when CR > 0.10.

    Parameters
    ----------
    matrix : np.ndarray
        nxn positive reciprocal pairwise comparison matrix.

    Returns
    -------
    AHPResult
        Same as ``ahp_weights`` when CR <= 0.10.

    Raises
    ------
    AHPError
        Structural validation failures (see ``ahp_weights``).
    InconsistencyError
        When the computed CR exceeds 0.10.  The error carries ``.cr`` and
        ``.most_inconsistent`` for UI use.
    """
    result = ahp_weights(matrix)
    if not result.consistent:
        most_inc = result.most_inconsistent
        assert most_inc is not None  # n >= 3 whenever CR can be > 0
        raise InconsistencyError(cr=result.cr, most_inconsistent=most_inc)
    return result
