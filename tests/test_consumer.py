"""Tests for the consumer rooftop-PV mode (Track B).

Proves: (1) the energy engine is correct and physically bounded; (2) it CANNOT
produce a utility-scale figure; (3) money outputs stay None (never fabricated)
when their inputs are unverified; (4) when real inputs ARE supplied the
arithmetic (payback / NPV) is correct.
"""

from __future__ import annotations

import math

import pytest

from solarsite.consumer import (
    RECOMMENDED_RANGES,
    ConsumptionInput,
    EconomicInputs,
    RoofInput,
    analyze_rooftop,
    compute_economics,
    energy_balance,
    roof_capacity_kwp,
)

# ---------------------------------------------------------------------------
# Capacity & production
# ---------------------------------------------------------------------------


def test_roof_capacity_from_area_and_efficiency() -> None:
    """100 m² x 0.75 usable x 0.20 efficiency = 15 kWp."""
    cap = roof_capacity_kwp(RoofInput(area_m2=100.0))
    assert cap == pytest.approx(15.0)


def test_capacity_never_utility_scale() -> None:
    """Even a giant 2000 m² roof stays a building-scale system, not a power plant.

    The whole point of the consumer mode: capacity comes from roof area, so it is
    physically incapable of the 61,557-GWh "carpet the region" figure.
    """
    cap = roof_capacity_kwp(RoofInput(area_m2=2000.0))
    assert cap == pytest.approx(300.0)  # kWp, i.e. 0.3 MWp — a big roof, not a farm
    prod_gwh = (cap * 1800.0) / 1e6  # capacity * specific yield -> GWh
    assert prod_gwh < 1.0  # 0.54 GWh — ~5 orders of magnitude below the 61,557 GWh case


def test_production_scales_with_yield() -> None:
    cap = roof_capacity_kwp(RoofInput(area_m2=50.0))  # 7.5 kWp
    res = analyze_rooftop(RoofInput(area_m2=50.0), specific_yield_kwh_kwp_yr=1600.0)
    assert res.energy.annual_production_kwh == pytest.approx(cap * 1600.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Energy balance / dispatch
# ---------------------------------------------------------------------------


def test_annual_net_metering_balance() -> None:
    """Net metering: self-consume up to consumption, export the surplus."""
    cap = 7.5
    bal = energy_balance(cap, 1600.0, ConsumptionInput(annual_kwh=8000.0))
    prod = cap * 1600.0  # 12,000 kWh
    assert bal.annual_production_kwh == pytest.approx(prod, rel=1e-6)
    assert bal.self_consumed_kwh == pytest.approx(8000.0)  # capped at consumption
    assert bal.exported_kwh == pytest.approx(prod - 8000.0)
    assert bal.grid_import_kwh == pytest.approx(0.0)
    assert bal.dispatch_policy == "annual_net_metering"


def test_self_consumption_fraction_dispatch() -> None:
    bal = energy_balance(
        7.5, 1600.0, ConsumptionInput(annual_kwh=8000.0, self_consumption_fraction=0.3)
    )
    prod = 12000.0
    assert bal.self_consumed_kwh == pytest.approx(0.3 * prod)  # 3600, < consumption
    assert bal.exported_kwh == pytest.approx(prod - 0.3 * prod)
    assert bal.dispatch_policy == "instantaneous_self_consumption"


# ---------------------------------------------------------------------------
# Economics: NO fabrication when inputs are unverified
# ---------------------------------------------------------------------------


def test_economics_none_when_inputs_unverified() -> None:
    """With default (stubbed) economics, NO monetary output is invented."""
    res = analyze_rooftop(
        RoofInput(area_m2=80.0),
        specific_yield_kwh_kwp_yr=1700.0,
        consumption=ConsumptionInput(annual_kwh=6000.0),
    )
    e = res.economics
    assert e.install_cost_usd is None
    assert e.annual_savings_usd is None
    assert e.simple_payback_years is None
    assert e.npv_usd is None
    # And the missing inputs are explicitly named, not silently dropped.
    assert "install_cost_usd_per_w" in e.unverified_inputs
    assert "retail_tariff_usd_per_kwh" in e.unverified_inputs
    assert any("not verified" in c for c in e.caveats)
    # Energy is still fully real.
    assert res.energy.annual_production_kwh > 0
    assert res.sanity_ok


def test_economics_computed_when_inputs_supplied() -> None:
    """With real inputs the arithmetic is exact and deterministic."""
    bal = energy_balance(10.0, 1500.0, ConsumptionInput(annual_kwh=15000.0))
    # production = 15,000 kWh; self-consumed = 15,000; exported = 0.
    econ = EconomicInputs(
        install_cost_usd_per_w=3.0,
        retail_tariff_usd_per_kwh=0.20,
        export_rate_usd_per_kwh=0.05,
        discount_rate=0.05,
        analysis_years=25,
        degradation_per_yr=0.0,
    )
    e = compute_economics(bal, econ)
    assert e.install_cost_usd == pytest.approx(10.0 * 1000.0 * 3.0)  # 30,000
    assert e.annual_savings_usd == pytest.approx(15000.0 * 0.20)  # 3,000 (no export)
    assert e.simple_payback_years == pytest.approx(30000.0 / 3000.0)  # 10 yr
    # NPV: -30000 + 3000 * annuity(5%,25). Annuity factor ~14.0939.
    annuity = sum(1.0 / 1.05**t for t in range(1, 26))
    assert e.npv_usd == pytest.approx(-30000.0 + 3000.0 * annuity, rel=1e-6)
    assert e.unverified_inputs == ["incentive_usd", "om_cost_usd_per_kw_yr"]


def test_incentive_reduces_net_cost() -> None:
    bal = energy_balance(8.0, 1600.0, ConsumptionInput(annual_kwh=10000.0))
    econ = EconomicInputs(
        install_cost_usd_per_w=3.5, retail_tariff_usd_per_kwh=0.18, incentive_usd=5000.0
    )
    e = compute_economics(bal, econ)
    assert e.install_cost_usd == pytest.approx(8.0 * 1000.0 * 3.5)  # 28,000
    assert e.net_install_cost_usd == pytest.approx(28000.0 - 5000.0)  # 23,000


# ---------------------------------------------------------------------------
# Sanity gate + ledger
# ---------------------------------------------------------------------------


def test_result_is_sanity_checked_and_bounded() -> None:
    res = analyze_rooftop(RoofInput(area_m2=120.0), specific_yield_kwh_kwp_yr=1800.0)
    assert res.sanity_ok, res.sanity_messages
    # capacity 120*0.75*0.20 = 18 kWp; well under the 0.25 kWp/m² ceiling (30 kWp).
    assert res.energy.capacity_kwp == pytest.approx(18.0)


def test_assumptions_ledger_lists_stubs() -> None:
    res = analyze_rooftop(RoofInput(area_m2=60.0), specific_yield_kwh_kwp_yr=1700.0)
    joined = " ".join(res.assumptions)
    assert "usable roof fraction" in joined
    assert "Economic inputs still needed" in joined


def test_no_utility_defaults_leak_in() -> None:
    """The consumer mode must not carry the utility 45 MWp/km² or $1000/kWp."""
    res = analyze_rooftop(RoofInput(area_m2=100.0), specific_yield_kwh_kwp_yr=1600.0)
    # 100 m² roof -> 15 kWp -> 24,000 kWh/yr. A utility 45 MWp/km² leak would be
    # orders of magnitude larger.
    assert res.energy.capacity_kwp == pytest.approx(15.0)
    assert res.energy.annual_production_kwh == pytest.approx(15.0 * 1600.0)


def test_recommended_ranges_have_sources() -> None:
    """Every recommended range cites a real source (no bare numbers)."""
    for name, info in RECOMMENDED_RANGES.items():
        assert info["source"].strip(), f"{name}: missing source"
        assert info["caveat"].strip(), f"{name}: missing caveat"


def test_zero_consumption_exports_all() -> None:
    """No consumption supplied -> all production exported, ratios sane (no div0)."""
    bal = energy_balance(5.0, 1600.0, ConsumptionInput())
    assert bal.self_consumed_kwh == pytest.approx(0.0)
    assert bal.exported_kwh == pytest.approx(5.0 * 1600.0)
    assert bal.self_consumption_ratio == pytest.approx(0.0)
    assert not math.isnan(bal.self_sufficiency)


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_api_consumer_rooftop_endpoint() -> None:
    """POST /consumer/rooftop returns real energy and null (not fake) economics."""
    from fastapi.testclient import TestClient

    from solarsite.api.app import app

    client = TestClient(app)
    resp = client.post(
        "/consumer/rooftop",
        json={
            "roof": {"area_m2": 90.0},
            "specific_yield_kwh_kwp_yr": 1700.0,
            "consumption": {"annual_kwh": 7000.0},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["energy"]["capacity_kwp"] == pytest.approx(90.0 * 0.75 * 0.20)
    assert body["energy"]["annual_production_kwh"] > 0
    # Economics stubbed -> null, with inputs named.
    assert body["economics"]["install_cost_usd"] is None
    assert "install_cost_usd_per_w" in body["economics"]["unverified_inputs"]
    assert body["sanity_ok"] is True


def test_api_recommended_ranges_endpoint() -> None:
    from fastapi.testclient import TestClient

    from solarsite.api.app import app

    client = TestClient(app)
    resp = client.get("/consumer/recommended-ranges")
    assert resp.status_code == 200
    body = resp.json()
    assert "install_cost_usd_per_w" in body
    assert body["install_cost_usd_per_w"]["source"]


# ---------------------------------------------------------------------------
# Validation-grade production + monthly profile + uncertainty band (Step 2/3.1)
# ---------------------------------------------------------------------------


def _diurnal_tmy() -> object:
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2005-01-01", periods=8760, freq="h", tz="UTC")
    hour = np.array([ts.hour for ts in idx], dtype=float)
    day = np.clip(np.sin((hour - 6.0) / 12.0 * np.pi), 0.0, None)
    return pd.DataFrame(
        {
            "ghi": 900.0 * day,
            "dni": 700.0 * day,
            "dhi": 200.0 * day,
            "temp_air": np.full(8760, 25.0),
            "wind_speed": np.full(8760, 2.0),
        },
        index=idx,
    )


def test_specific_yield_with_profile_sums_to_annual() -> None:
    from solarsite.analysis.energy import EnergyAssumptions, specific_yield_with_profile

    annual, monthly = specific_yield_with_profile(31.2, 27.5, _diurnal_tmy(), EnergyAssumptions())
    assert len(monthly) == 12
    assert sum(monthly) == pytest.approx(annual, rel=1e-6)
    assert annual > 0


def test_location_production_with_injected_tmy() -> None:
    from solarsite.consumer.production import location_production

    prod = location_production(31.2, 27.5, tmy_df=_diurnal_tmy(), compute_interannual=False)
    assert prod.method == "pvlib_modelchain"
    assert len(prod.monthly_kwh_per_kwp) == 12
    assert prod.specific_yield_kwh_kwp_yr > 0
    assert prod.surface_azimuth == pytest.approx(180.0)  # northern hemisphere
    # Phase B: an optimal-orientation comparison is always computed (no extra network).
    assert 0.0 < prod.orientation_ratio <= 1.0001
    assert prod.optimal_specific_yield_kwh_kwp_yr >= prod.specific_yield_kwh_kwp_yr - 1e-6
    assert prod.p90_specific_yield_kwh_kwp_yr is None  # interannual disabled here


def test_analyze_rooftop_with_profile_and_band() -> None:
    monthly_per_kwp = [100.0] * 12  # 1200 kWh/kWp/yr
    res = analyze_rooftop(
        RoofInput(area_m2=100.0),  # 15 kWp
        specific_yield_kwh_kwp_yr=1200.0,
        consumption=ConsumptionInput(annual_kwh=10000.0),
        econ=EconomicInputs(install_cost_usd_per_w=3.0, retail_tariff_usd_per_kwh=0.20),
        monthly_kwh_per_kwp=monthly_per_kwp,
        production_method="pvlib_modelchain",
    )
    # Monthly system kWh = monthly/kWp * capacity (15 kWp).
    assert res.monthly_kwh is not None and len(res.monthly_kwh) == 12
    assert res.monthly_kwh[0] == pytest.approx(100.0 * 15.0, rel=1e-6)
    assert res.production_method == "pvlib_modelchain"
    assert "validation-grade" in (res.production_note or "")
    # Payback band brackets the point payback.
    assert res.payback_band is not None
    pb = res.economics.simple_payback_years
    assert res.payback_band.low <= pb <= res.payback_band.high
    # The unverified panel lists the missing economic inputs (export/incentive/O&M).
    assert any("export_rate_usd_per_kwh" in s for s in res.unverified_panel)


def test_api_consumer_rooftop_validation_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST with lat/lon computes a validation-grade result (PVGIS mocked offline)."""
    from fastapi.testclient import TestClient

    import solarsite.api.app as appmod
    from solarsite.consumer.production import LocationProduction

    def _fake_location_production(lat: float, lon: float, **kwargs: object) -> LocationProduction:
        return LocationProduction(
            latitude=lat,
            longitude=lon,
            specific_yield_kwh_kwp_yr=1800.0,
            monthly_kwh_per_kwp=[150.0] * 12,
            surface_tilt=abs(lat),
            surface_azimuth=180.0,
            method="pvlib_modelchain",
            shading_pct=3.0,
            optimal_tilt=abs(lat),
            optimal_azimuth=180.0,
            optimal_specific_yield_kwh_kwp_yr=1850.0,
            orientation_ratio=1800.0 / 1850.0,
            p50_specific_yield_kwh_kwp_yr=1800.0,
            p90_specific_yield_kwh_kwp_yr=1650.0,
            interannual_note="P90 from PVGIS interannual variability (SD_y/E_y = 6.0%).",
        )

    monkeypatch.setattr(appmod, "location_production", _fake_location_production)
    client = TestClient(appmod.app)
    resp = client.post(
        "/consumer/rooftop",
        json={
            "roof": {"area_m2": 90.0},
            "latitude": 31.2,
            "longitude": 27.5,
            "consumption": {"annual_kwh": 7000.0},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["production_method"] == "pvlib_modelchain"
    assert len(body["monthly_kwh"]) == 12
    assert body["energy"]["specific_yield_kwh_kwp_yr"] == pytest.approx(1800.0)


@pytest.mark.live
def test_live_location_production_pvgis() -> None:
    """Real PVGIS fetch for NW Egypt coast yields a plausible validation-grade number."""
    from solarsite.consumer.production import location_production

    prod = location_production(31.1, 27.5)
    assert prod.method == "pvlib_modelchain"
    assert 1400.0 <= prod.specific_yield_kwh_kwp_yr <= 2200.0
    assert sum(prod.monthly_kwh_per_kwp) == pytest.approx(prod.specific_yield_kwh_kwp_yr, rel=1e-3)


def test_orientation_penalty_for_bad_tilt() -> None:
    """A flat (tilt=0) roof in a sunny mid-latitude yields LESS than the optimum."""
    from solarsite.consumer.production import location_production

    flat = location_production(
        31.2, 27.5, tmy_df=_diurnal_tmy(), surface_tilt=0.0, compute_interannual=False
    )
    assert flat.orientation_ratio < 1.0  # flat is below the swept optimum
    assert flat.surface_tilt == pytest.approx(0.0)


def test_shading_reduces_yield_no_double_count() -> None:
    """User shading replaces the flat 3% — 20% shading yields less than the 3% default."""
    from solarsite.consumer.production import location_production

    base = location_production(31.2, 27.5, tmy_df=_diurnal_tmy(), compute_interannual=False)
    shaded = location_production(
        31.2, 27.5, tmy_df=_diurnal_tmy(), shading_pct=20.0, compute_interannual=False
    )
    assert shaded.shading_pct == pytest.approx(20.0)
    assert shaded.specific_yield_kwh_kwp_yr < base.specific_yield_kwh_kwp_yr


def test_p90_from_injected_interannual_cv() -> None:
    """A supplied interannual CV produces a real, labelled P90 (no manufacturing)."""
    from solarsite.consumer.production import location_production

    prod = location_production(
        31.2, 27.5, tmy_df=_diurnal_tmy(), interannual_cv=0.06, compute_interannual=False
    )
    assert prod.p90_specific_yield_kwh_kwp_yr is not None
    # P90 = P50 * (1 - 1.2816*0.06) < P50
    assert prod.p90_specific_yield_kwh_kwp_yr < prod.p50_specific_yield_kwh_kwp_yr
    assert "interannual" in prod.interannual_note.lower()


def test_analyze_rooftop_huge_roof_flags_not_silently_passes() -> None:
    """The 35-MWp hole: a 234,854 m² roof must NOT silently produce a valid headline."""
    res = analyze_rooftop(RoofInput(area_m2=234854.0), specific_yield_kwh_kwp_yr=1800.0)
    assert not res.sanity_ok, "an implausible roof must fail the sanity verdict"
    assert res.warnings, "and surface a friendly warning"


def test_analyze_rooftop_normal_roof_no_warnings() -> None:
    res = analyze_rooftop(RoofInput(area_m2=120.0), specific_yield_kwh_kwp_yr=1800.0)
    assert res.sanity_ok
    assert res.warnings == []
