"""Tests for the uncertainty/sensitivity feature (Track C)."""

from __future__ import annotations

import pytest

from solarsite.uncertainty import propagate, tornado


def _payback(cost_per_w: float, kwp: float, tariff: float, annual_kwh_per_kwp: float) -> float:
    """Simple payback (yr) = install cost / annual bill savings."""
    install = cost_per_w * kwp * 1000.0
    annual_savings = kwp * annual_kwh_per_kwp * tariff
    return install / annual_savings


def test_propagate_band_brackets_base() -> None:
    """The band's low ≤ base ≤ high, and corners are exact for a monotonic fn."""
    band = propagate(
        _payback,
        {
            "cost_per_w": (3.0, 4.0, 5.0),
            "kwp": (8.0, 8.0, 8.0),  # fixed
            "tariff": (0.15, 0.20, 0.25),
            "annual_kwh_per_kwp": (1600.0, 1600.0, 1600.0),  # fixed
        },
    )
    assert band.low <= band.base <= band.high
    # Cheapest+highest-tariff = fastest payback (low); dearest+lowest-tariff = slowest.
    fast = _payback(3.0, 8.0, 0.25, 1600.0)
    slow = _payback(5.0, 8.0, 0.15, 1600.0)
    assert band.low == pytest.approx(fast)
    assert band.high == pytest.approx(slow)
    assert band.spread > 0


def test_tornado_orders_by_swing() -> None:
    """Tornado rows are sorted by swing descending (dominant driver first)."""
    rows = tornado(
        _payback,
        {
            "cost_per_w": (3.0, 4.0, 5.0),  # ±25% around base
            "kwp": (8.0, 8.0, 8.0),  # no range -> zero swing
            "tariff": (0.10, 0.20, 0.40),  # large range -> big swing
            "annual_kwh_per_kwp": (1500.0, 1600.0, 1700.0),
        },
    )
    swings = [r.swing for r in rows]
    assert swings == sorted(swings, reverse=True)
    # The fixed parameter contributes zero swing.
    kwp_row = next(r for r in rows if r.parameter == "kwp")
    assert kwp_row.swing == pytest.approx(0.0)
    # Tariff's wide range should dominate cost here.
    assert rows[0].parameter in {"tariff", "cost_per_w"}


def test_propagate_rejects_too_many_params() -> None:
    with pytest.raises(ValueError, match="too many uncertain params"):
        propagate(lambda **_: 0.0, {f"p{i}": (0.0, 1.0, 2.0) for i in range(13)})


def test_band_exact_when_single_param() -> None:
    band = propagate(lambda x: 2.0 * x, {"x": (1.0, 2.0, 3.0)})
    assert (band.low, band.base, band.high) == pytest.approx((2.0, 4.0, 6.0))
