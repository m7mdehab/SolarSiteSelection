"""Tests for the itemized PV loss stack (E2).

The headline assertion is an INDEPENDENT-ORACLE check: our hand-entered PVWatts
component defaults must reproduce ``pvlib.pvsystem.pvwatts_losses()`` exactly.
That catches any transcription error in the component table without trusting our
own arithmetic.
"""

from __future__ import annotations

import pytest
from pvlib.pvsystem import pvwatts_losses

from solarsite.analysis.losses import DC_LOSS_FIELDS, LossStack


def test_dc_total_matches_pvlib_oracle() -> None:
    """Our 10 DC components must reproduce pvlib's documented default total."""
    stack = LossStack()
    # pvlib returns percent; our property returns a fraction.
    pvlib_pct = float(pvwatts_losses())  # type: ignore[no-untyped-call]
    assert stack.dc_loss_fraction * 100.0 == pytest.approx(pvlib_pct, abs=1e-9)
    # And the documented PVWatts default is ~14.08%.
    assert stack.dc_loss_fraction * 100.0 == pytest.approx(14.0757, abs=1e-3)


def test_components_feed_pvlib_identically() -> None:
    """Passing our components into pvlib yields the same number (no drift)."""
    stack = LossStack(soiling=5.0, shading=1.0, mismatch=3.0)
    comps = stack.dc_components()
    # pvlib uses 'nameplate_rating' — our field name matches its kwarg names.
    pvlib_pct = float(pvwatts_losses(**comps))  # type: ignore[arg-type]
    assert stack.dc_loss_fraction * 100.0 == pytest.approx(pvlib_pct, abs=1e-9)


def test_multiplicative_not_additive() -> None:
    """Losses combine multiplicatively: 1 - prod(1-Li), never the naive sum."""
    stack = LossStack(
        soiling=10.0,
        shading=10.0,
        snow=0.0,
        mismatch=0.0,
        wiring=0.0,
        connections=0.0,
        lid=0.0,
        nameplate_rating=0.0,
        age=0.0,
        availability=0.0,
    )
    # Naive sum would be 20%; multiplicative is 1 - 0.9*0.9 = 19%.
    assert stack.dc_loss_fraction == pytest.approx(0.19, abs=1e-9)


def test_total_derate_includes_inverter() -> None:
    """total_derate = dc_derate * inverter_nominal_efficiency."""
    stack = LossStack()
    assert stack.total_derate == pytest.approx(stack.dc_derate * 0.96, abs=1e-12)
    # End-to-end loss for the defaults: 1 - (1-0.1408)*0.96 ≈ 17.5%.
    assert stack.total_loss_fraction == pytest.approx(0.1752, abs=1e-3)


def test_line_items_cover_every_component_plus_inverter() -> None:
    """The surfaced breakdown has one row per DC component + one inverter row."""
    rows = LossStack().line_items()
    dc_rows = [r for r in rows if r.kind == "dc"]
    inv_rows = [r for r in rows if r.kind == "inverter"]
    assert {r.name for r in dc_rows} == set(DC_LOSS_FIELDS)
    assert len(inv_rows) == 1
    assert inv_rows[0].percent == pytest.approx(4.0, abs=1e-6)  # 1 - 0.96


def test_derates_are_bounded() -> None:
    """Derate properties stay within [0, 1] across a sweep of inputs."""
    for s in (0.0, 5.0, 50.0):
        stack = LossStack(soiling=s, shading=s, mismatch=s)
        assert 0.0 <= stack.dc_loss_fraction <= 1.0
        assert 0.0 <= stack.dc_derate <= 1.0
        assert 0.0 <= stack.total_derate <= 1.0


def test_inverter_efficiency_validation() -> None:
    """Implausible inverter efficiency is rejected."""
    with pytest.raises(ValueError, match="plausible"):
        LossStack(inverter_nominal_efficiency=0.5)
