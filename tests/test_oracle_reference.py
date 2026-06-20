"""Reference-case regression against the PVGIS PVcalc oracle (Track F).

The cached fixture (``fixtures/oracle/pvgis_pvcalc_reference.json``) holds REAL
specific yields fetched from PVGIS for four known sites — including a SOUTHERN-
hemisphere site (Cape Town) whose equator-facing optimum is due NORTH, directly
corroborating the E1 fix.

Offline tests (CI) assert the oracle values land inside our physical envelopes
(calibration: real data must not be flagged) and that the envelopes would catch a
wrong number. The live test (network, excluded from CI) re-fetches PVGIS to detect
oracle drift and cross-checks our pvlib ModelChain.

The SECOND oracle (NREL PVWatts) is not yet wired — it needs an NREL API key the
build host lacks (a known follow-up); PVGIS is oracle #1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solarsite.validation import (
    capacity_factor_from_specific_yield,
    check,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "oracle" / "pvgis_pvcalc_reference.json"


def _load_reference() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_reference_fixture_has_provenance() -> None:
    """The oracle fixture must carry full provenance (no anonymous numbers)."""
    ref = _load_reference()
    prov = ref["_provenance"]
    assert "pvgis" in prov["source"].lower()
    assert prov["endpoint"].startswith("https://")
    assert prov["fetched_as_of"]
    assert len(ref["sites"]) >= 4


@pytest.mark.parametrize("site", _load_reference()["sites"], ids=lambda s: s["name"])
def test_reference_specific_yields_within_envelope(site: dict) -> None:
    """Every real PVGIS reference yield sits inside the physical envelope.

    This calibrates the Track F bounds against reality: if a true measured value
    were flagged, the envelope would be wrong.
    """
    sy = site["E_y_kwh_per_kwp_yr"]
    sy_check = check("specific_yield_kwh_kwp_yr", sy)
    assert sy_check.ok, f"{site['name']}: {sy_check.message}"
    cf = capacity_factor_from_specific_yield(sy)
    cf_check = check("ac_capacity_factor", cf)
    assert cf_check.ok, f"{site['name']}: CF {cf:.3f} flagged — envelope too tight?"


def test_southern_hemisphere_reference_is_north_facing() -> None:
    """The southern-hemisphere reference uses an equator-facing (north) array.

    PVGIS aspect=180 = due north; this is the real-world corroboration of the E1
    fix (effective_azimuth(-33.93) -> 0° = north in pvlib's convention).
    """
    ref = _load_reference()
    south = [s for s in ref["sites"] if s["hemisphere"] == "S"]
    assert south, "fixture must include a southern-hemisphere reference"
    for s in south:
        assert s["aspect_pvgis"] == 180, f"{s['name']}: S-hemisphere should face north"


def test_envelope_would_catch_a_wrong_reference() -> None:
    """A corrupted reference (e.g. a x10 units bug) is caught by the envelope."""
    bogus = 1862.5 * 10  # 18,625 kWh/kWp/yr — impossible
    assert not check("specific_yield_kwh_kwp_yr", bogus).ok


@pytest.mark.live
def test_live_pvgis_matches_cached_reference_and_model() -> None:
    """Re-fetch PVGIS (oracle drift) + an APPLES-TO-APPLES pvlib cross-check.

    Network test, excluded from CI. Two documented checks per site:

    1. Oracle stability: PVGIS PVcalc (loss=14%, the cached fixture's assumption)
       must reproduce the cached E_y within **3%**.
    2. Model validation (matched loss): we set PVGIS PVcalc's ``loss`` equal to the
       model's OWN combined non-temperature system loss (DC stack 14.08% x inverter
       0.96 = 17.51%), so the comparison isolates the irradiance→POA→cell-temp→AC
       physics, NOT the loss bookkeeping. pvlib must then agree within **4%**.

    Executed 2026-06-21 (recorded in DECISIONS): matched-loss agreement was
    Aswan 2.2%, NW Egypt 0.6%, Munich 1.4%, Cape Town 0.7% — worst 2.2%. The 4%
    threshold leaves margin for genuine model differences without absorbing a bug;
    it was NOT widened to pass (observed << threshold).
    """
    import httpx
    import pandas as pd

    from solarsite.analysis.energy import EnergyAssumptions, specific_yield

    assumptions = EnergyAssumptions()
    matched_loss_pct = round(assumptions.loss_stack.total_loss_fraction * 100.0, 2)

    def _pvcalc(lat: float, lon: float, aspect: int, loss: float) -> float:
        r = httpx.get(
            "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc",
            params={
                "lat": lat,
                "lon": lon,
                "peakpower": 1,
                "loss": loss,
                "mountingplace": "free",
                "angle": abs(lat),
                "aspect": aspect,
                "outputformat": "json",
            },
            timeout=120.0,
        )
        r.raise_for_status()
        return float(r.json()["outputs"]["totals"]["fixed"]["E_y"])

    ref = _load_reference()
    for site in ref["sites"]:
        lat, lon, aspect = site["lat"], site["lon"], site["aspect_pvgis"]

        # 1. oracle stability vs the cached (loss=14) reference
        fresh14 = _pvcalc(lat, lon, aspect, 14)
        cached = site["E_y_kwh_per_kwp_yr"]
        assert abs(fresh14 - cached) / cached <= 0.03, (
            f"{site['name']}: PVGIS drift {fresh14:.0f} vs cached {cached:.0f}"
        )

        # 2. matched-loss model cross-check (apples-to-apples)
        pvcalc_matched = _pvcalc(lat, lon, aspect, matched_loss_pct)
        tmy = httpx.get(
            "https://re.jrc.ec.europa.eu/api/v5_2/tmy",
            params={"lat": lat, "lon": lon, "outputformat": "json"},
            timeout=120.0,
        )
        tmy.raise_for_status()
        hourly = tmy.json()["outputs"]["tmy_hourly"]
        df = pd.DataFrame(hourly)
        ts = df["time(UTC)"].str.replace(":", "", regex=False)
        df.index = pd.to_datetime(ts, format="%Y%m%d%H%M", utc=True)
        df = df.drop(columns=["time(UTC)"])
        sy = specific_yield(lat=lat, lon=lon, tmy_df=df, assumptions=assumptions)
        disagreement = abs(sy - pvcalc_matched) / pvcalc_matched
        assert disagreement <= 0.04, (
            f"{site['name']}: pvlib {sy:.0f} vs PVGIS@{matched_loss_pct}% "
            f"{pvcalc_matched:.0f} = {disagreement:.1%} > 4% (matched loss)"
        )
