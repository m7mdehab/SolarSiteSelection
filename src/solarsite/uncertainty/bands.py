"""Uncertainty & sensitivity as a feature (Track C).

Turns a point estimate into a confidence BAND and a tornado sensitivity, given
the caller's input RANGES. This invents nothing: it only propagates ranges the
caller supplies (e.g. the published cost/tariff ranges a human chooses to explore),
so it pairs with the honesty story — a single number becomes "low / base / high".

Method: interval propagation by corner evaluation. For a function that is
monotonic in each parameter over its range (true for payback = cost/savings, NPV,
specific yield x area, ...) the 2^k corner combinations contain the true min and
max, so the band is exact. For a non-monotonic response the band is an
approximation from the corners + base point (documented, not hidden).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping

from pydantic import BaseModel, Field

__all__ = ["Band", "TornadoRow", "propagate", "tornado"]

#: Guard against combinatorial blow-up: 2^MAX_PARAMS corner evaluations.
MAX_PARAMS: int = 12


class Band(BaseModel):
    """A low / base / high confidence band for one output."""

    low: float
    base: float
    high: float

    @property
    def spread(self) -> float:
        """Absolute width of the band (high - low)."""
        return self.high - self.low


class TornadoRow(BaseModel):
    """One parameter's one-at-a-time effect on the output (others held at base)."""

    parameter: str = Field(..., description="Which input was varied.")
    low_output: float = Field(..., description="Output with this input at its low.")
    high_output: float = Field(..., description="Output with this input at its high.")
    swing: float = Field(..., description="abs(high_output - low_output) — the bar length.")


def _bases(params: Mapping[str, tuple[float, float, float]]) -> dict[str, float]:
    return {k: float(v[1]) for k, v in params.items()}


def propagate(
    fn: Callable[..., float],
    params: Mapping[str, tuple[float, float, float]],
) -> Band:
    """Propagate input ranges to an output :class:`Band`.

    ``params`` maps each keyword argument of ``fn`` to ``(low, base, high)``.
    Returns the base output (all inputs at base) plus the min/max output over the
    2^k low/high corner combinations.
    """
    if len(params) > MAX_PARAMS:
        raise ValueError(f"too many uncertain params ({len(params)} > {MAX_PARAMS})")

    base = float(fn(**_bases(params)))
    keys = list(params.keys())
    lows_highs = [(params[k][0], params[k][2]) for k in keys]

    lo = hi = base
    for combo in itertools.product(*lows_highs):
        out = float(fn(**dict(zip(keys, combo, strict=True))))
        lo = min(lo, out)
        hi = max(hi, out)
    return Band(low=lo, base=base, high=hi)


def tornado(
    fn: Callable[..., float],
    params: Mapping[str, tuple[float, float, float]],
) -> list[TornadoRow]:
    """One-at-a-time sensitivity: vary each input low→high with others at base.

    Returns rows sorted by ``swing`` descending — the classic tornado order, so
    the dominant driver of uncertainty is first.
    """
    bases = _bases(params)
    rows: list[TornadoRow] = []
    for k, (lo, _base, hi) in params.items():
        out_lo = float(fn(**{**bases, k: float(lo)}))
        out_hi = float(fn(**{**bases, k: float(hi)}))
        rows.append(
            TornadoRow(
                parameter=k,
                low_output=out_lo,
                high_output=out_hi,
                swing=abs(out_hi - out_lo),
            )
        )
    rows.sort(key=lambda r: r.swing, reverse=True)
    return rows
