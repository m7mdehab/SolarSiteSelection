"""Tests for the physical-sanity bounds gate (Track F).

These prove two things at once:
1. Real, correctly-computed PV numbers pass every envelope (no false alarms).
2. Genuinely impossible numbers (300% CF, $50/W, 5000 kWh/kWp/yr, a roof packing
   more than 0.25 kWp/m²) are CAUGHT — so a fabricated/units-bug figure cannot
   silently ship.
"""

from __future__ import annotations

import pytest

from solarsite.analysis.energy import EnergyAssumptions, site_energy_from_ghi
from solarsite.validation import (
    PHYSICAL_BOUNDS,
    SanityViolation,
    assert_within,
    capacity_factor_from_specific_yield,
    check,
    check_energy_result,
    check_roof_capacity,
)

# ---------------------------------------------------------------------------
# Real numbers pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ghi", [1600.0, 2000.0, 2200.0])
def test_offline_utility_result_within_bounds(ghi: float) -> None:
    """A realistic offline GHI*PR result passes every applicable envelope."""
    res = site_energy_from_ghi(ghi, area_km2=1.0)
    checks = check_energy_result(
        res.specific_yield_kwh_kwp_yr,
        area_km2=1.0,
        capacity_mwp=res.capacity_mwp,
        lcoe_usd_per_mwh=res.lcoe_usd_per_mwh,
    )
    bad = [c for c in checks if not c.ok]
    assert not bad, f"unexpected sanity violations: {[c.message for c in bad]}"


def test_huge_area_is_not_a_sanity_violation() -> None:
    """The 61,557-GWh case is a PRESENTATION problem, not an insane number.

    A correct computation over an 834 km² site yields ~61,557 GWh — but every
    per-unit quantity (specific yield, capacity factor, power density) is sane.
    The sanity gate must NOT fire here; the fix is the consumer/per-site framing,
    not clamping a physically-correct total.
    """
    a = EnergyAssumptions()
    # GHI ~2186 kWh/m²/yr * PR 0.75 -> specific yield ~1640 (the original case).
    res = site_energy_from_ghi(2186.0, area_km2=834.0, assumptions=a)
    assert res.annual_gwh > 50_000  # the large total is real
    checks = check_energy_result(
        res.specific_yield_kwh_kwp_yr,
        area_km2=834.0,
        capacity_mwp=res.capacity_mwp,
        lcoe_usd_per_mwh=res.lcoe_usd_per_mwh,
    )
    assert all(c.ok for c in checks), [c.message for c in checks if not c.ok]


# ---------------------------------------------------------------------------
# Impossible numbers are caught
# ---------------------------------------------------------------------------


def test_specific_yield_too_high_is_caught() -> None:
    c = check("specific_yield_kwh_kwp_yr", 5000.0)
    assert not c.ok
    assert "outside physical envelope" in c.message


def test_capacity_factor_over_unity_is_caught() -> None:
    # sy=5000 -> CF ~0.57, impossible for PV.
    cf = capacity_factor_from_specific_yield(5000.0)
    assert cf > 0.40
    assert not check("ac_capacity_factor", cf).ok


def test_absurd_power_density_is_caught() -> None:
    assert not check("power_density_mwp_per_km2", 500.0).ok


def test_absurd_residential_cost_is_caught() -> None:
    assert not check("residential_install_cost_usd_per_w", 50.0).ok


def test_assert_within_raises_on_violation() -> None:
    with pytest.raises(SanityViolation, match="outside physical envelope"):
        assert_within("specific_yield_kwh_kwp_yr", 9999.0)


def test_assert_within_passes_on_good_value() -> None:
    assert_within("specific_yield_kwh_kwp_yr", 1800.0)  # no raise


# ---------------------------------------------------------------------------
# Per-roof capacity ceiling (makes over-stated rooftop capacity impossible)
# ---------------------------------------------------------------------------


def test_roof_capacity_within_ceiling_ok() -> None:
    # 100 m² roof, 15 kWp -> 0.15 kWp/m², under the 0.25 ceiling.
    assert check_roof_capacity(100.0, 15.0).ok


def test_roof_capacity_over_ceiling_caught() -> None:
    # 100 m² roof, 30 kWp -> 0.30 kWp/m², over the 0.25 physical ceiling.
    c = check_roof_capacity(100.0, 30.0)
    assert not c.ok
    assert "exceeds physical roof ceiling" in c.message


# ---------------------------------------------------------------------------
# Reference cases: known-site specific yields land in the envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("site", "sy"),
    [
        ("Aswan desert (high)", 2100.0),
        ("NW Egypt coast", 1800.0),
        ("Germany (moderate)", 1000.0),
        ("UK (low)", 850.0),
    ],
)
def test_reference_specific_yields_in_envelope(site: str, sy: float) -> None:
    """Published-order-of-magnitude specific yields for known regions are sane."""
    c = check("specific_yield_kwh_kwp_yr", sy)
    assert c.ok, f"{site}: {c.message}"
    cf = capacity_factor_from_specific_yield(sy)
    assert check("ac_capacity_factor", cf).ok, f"{site}: CF {cf:.3f} out of envelope"


def test_every_registered_bound_is_self_consistent() -> None:
    """Each bound has lo < hi and a non-empty source (no placeholder envelopes)."""
    for name, b in PHYSICAL_BOUNDS.items():
        assert b.lo < b.hi, f"{name}: lo>=hi"
        assert b.source.strip(), f"{name}: empty source"


# ---------------------------------------------------------------------------
# Absolute consumer guardrails (close the "huge roof passes density-only" hole)
# ---------------------------------------------------------------------------


def test_consumer_plausibility_normal_roof_ok() -> None:
    from solarsite.validation import check_consumer_plausibility

    hard, warnings = check_consumer_plausibility(roof_area_m2=120.0, capacity_kwp=18.0)
    assert all(c.ok for c in hard)
    assert warnings == []


def test_consumer_plausibility_huge_roof_fails_hard() -> None:
    from solarsite.validation import check_consumer_plausibility

    # The exact failure class: a 234,854 m² "roof" -> ~35 MWp passes a 0.25 kWp/m²
    # density check but must be flagged hard here.
    hard, warnings = check_consumer_plausibility(roof_area_m2=234854.0, capacity_kwp=35228.0)
    assert any(not c.ok for c in hard), "huge roof/system must fail the hard envelope"
    assert warnings, "and must also carry a friendly warning"


def test_consumer_plausibility_commercial_roof_warns_not_fails() -> None:
    from solarsite.validation import check_consumer_plausibility

    # A big-but-real commercial roof: warned ("confirm"), not hard-failed.
    hard, warnings = check_consumer_plausibility(roof_area_m2=2500.0, capacity_kwp=375.0)
    assert all(c.ok for c in hard)
    assert any("larger than a typical building" in w for w in warnings)
