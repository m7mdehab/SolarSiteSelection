"""Validation-grade per-location production for the consumer mode (Steps 2/3.1).

Fetches the PVGIS Typical Meteorological Year for the user's location (cached via
the standard DiskCache) and runs the pvlib ModelChain to get a validation-grade
annual specific yield plus its monthly profile. This is the SAME physics validated
against the PVGIS oracle to <=2.2% (see tests/test_oracle_reference.py), so the
production numbers a consumer sees are validation-grade — not the cruder GHI*PR
offline estimate.

Phase B adds:
* the user's ACTUAL roof tilt + orientation (not just the equator-facing optimum),
* an optimal-vs-yours comparison (a local tilt sweep over the same TMY — no extra
  network), so the user sees the production penalty of their real orientation,
* honest shading as a user input that REPLACES the flat PVWatts 3% shading loss
  (never double-counted),
* P50/P90 from PVGIS's own interannual variability (PVcalc ``SD_y``) — best-effort;
  if unavailable, P90 is ``None`` and we say so rather than manufacture a spread.

Network: this calls PVGIS. It is therefore exercised only by live-marked tests;
unit tests inject a TMY DataFrame (and monkeypatch the interannual fetch) instead.
"""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd
from pydantic import BaseModel, Field

from solarsite.analysis.energy import (
    EnergyAssumptions,
    average_diurnal_profile,
    specific_yield_with_profile,
)
from solarsite.analysis.losses import LossStack

__all__ = ["LocationProduction", "location_production"]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# P90 (value exceeded in 90% of years) under a normal approximation: P50 - z*sigma,
# z = 1.2816 for the 90th-percentile exceedance.
_P90_Z = 1.2816

_PVGIS_PVCALC_URL = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"


class LocationProduction(BaseModel):
    """Validation-grade specific yield + monthly profile for one location/orientation."""

    latitude: float
    longitude: float
    specific_yield_kwh_kwp_yr: float
    monthly_kwh_per_kwp: list[float] = Field(..., description="12 values, Jan..Dec; sum=annual.")
    month_labels: list[str] = Field(default_factory=lambda: list(_MONTHS))
    surface_tilt: float
    surface_azimuth: float
    method: str = "pvlib_modelchain"
    # ---- Phase B: orientation comparison + interannual band -------------------
    shading_pct: float = Field(..., description="Shading loss applied (% — replaces the flat 3%).")
    optimal_tilt: float
    optimal_azimuth: float
    optimal_specific_yield_kwh_kwp_yr: float
    orientation_ratio: float = Field(..., description="yours / optimal (0..1).")
    p50_specific_yield_kwh_kwp_yr: float = Field(..., description="Typical-year (= the headline).")
    p90_specific_yield_kwh_kwp_yr: float | None = Field(
        default=None,
        description="90%-exceedance yield from PVGIS interannual SD_y; None if absent.",
    )
    interannual_note: str = ""
    diurnal_shape: list[float] = Field(
        default_factory=list, description="Average 24h generation shape (fractions, sum=1)."
    )


def _tiny_aoi(lat: float, lon: float, half: float = 0.02) -> Any:
    """A small square AOI around the point (PVGIS TMY uses the centroid)."""
    from solarsite.core import AOI

    return AOI.from_geojson(
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon - half, lat - half],
                    [lon + half, lat - half],
                    [lon + half, lat + half],
                    [lon - half, lat + half],
                    [lon - half, lat - half],
                ]
            ],
        }
    )


def _assumptions(
    tilt: float | None, azimuth: float | None, shading_pct: float | None
) -> EnergyAssumptions:
    """Build EnergyAssumptions honouring a user tilt/azimuth and shading override.

    Shading replaces the flat PVWatts ``shading`` component (default 3%) so the
    user's shading is never double-counted on top of the model default.
    """
    stack = LossStack() if shading_pct is None else LossStack(shading=float(shading_pct))
    return EnergyAssumptions(tilt=tilt, azimuth=azimuth, loss_stack=stack)


def _annual_yield(
    lat: float, lon: float, tmy_df: pd.DataFrame, tilt: float, azimuth: float, shading_pct: float
) -> float:
    a = _assumptions(tilt, azimuth, shading_pct)
    annual, _ = specific_yield_with_profile(lat, lon, tmy_df, a)
    return annual


def _optimal_orientation(
    lat: float, lon: float, tmy_df: pd.DataFrame, shading_pct: float
) -> tuple[float, float, float]:
    """Sweep fixed tilt over the SAME TMY (no network) to find the optimum.

    Azimuth optimum is equator-facing (well established: 180° N / 0° S); we sweep
    tilt 0-60° and return ``(optimal_tilt, optimal_azimuth, optimal_yield)``.
    """
    azimuth = 180.0 if lat >= 0 else 0.0
    best_tilt, best_yield = 0.0, -1.0
    for tilt in range(0, 61, 5):
        y = _annual_yield(lat, lon, tmy_df, float(tilt), azimuth, shading_pct)
        if y > best_yield:
            best_tilt, best_yield = float(tilt), y
    return best_tilt, azimuth, best_yield


def _fetch_interannual_cv(lat: float, lon: float, tilt: float, azimuth: float) -> float | None:
    """Interannual coefficient of variation (SD_y/E_y) from PVGIS PVcalc.

    PVGIS computes ``SD_y`` (the standard deviation of yearly production across its
    multi-year record) — a REAL interannual spread, not a manufactured one. Returns
    ``CV = SD_y/E_y`` or ``None`` on any failure (caller then omits P90). Isolated so
    tests monkeypatch it (no live call in CI).
    """
    # PVGIS aspect = degrees from south, -180..180 (0=S, -90=E, 90=W).
    aspect = azimuth - 180.0
    if aspect > 180.0:
        aspect -= 360.0
    resp = httpx.get(
        _PVGIS_PVCALC_URL,
        params={
            "lat": lat,
            "lon": lon,
            "peakpower": 1,
            "loss": 14,
            "angle": tilt,
            "aspect": aspect,
            "outputformat": "json",
        },
        timeout=8.0,
    )
    resp.raise_for_status()
    totals = resp.json()["outputs"]["totals"]["fixed"]
    e_y = float(totals["E_y"])
    sd_y = float(totals["SD_y"])
    if e_y <= 0:
        return None
    return sd_y / e_y


def location_production(
    lat: float,
    lon: float,
    assumptions: EnergyAssumptions | None = None,
    *,
    surface_tilt: float | None = None,
    surface_azimuth: float | None = None,
    shading_pct: float | None = None,
    tmy_df: pd.DataFrame | None = None,
    cache: Any | None = None,
    interannual_cv: float | None = None,
    compute_interannual: bool = True,
) -> LocationProduction:
    """Compute validation-grade production for a location AND the user's orientation.

    ``surface_tilt`` / ``surface_azimuth`` honour the user's real roof (defaults:
    tilt=|lat|, equator-facing). ``shading_pct`` overrides the flat 3% shading loss.
    If ``tmy_df`` is supplied (tests) it is used directly; otherwise the PVGIS TMY is
    fetched (cached). Raises on TMY failure — we never invent a production number.
    The interannual P90 is best-effort (PVGIS PVcalc ``SD_y``); ``None`` if absent.
    """
    if tmy_df is None:
        from solarsite.acquire.pvgis import PVGISSource

        tmy_df = PVGISSource(cache=cache).fetch_tmy(_tiny_aoi(lat, lon))

    # Resolve effective shading once (so the optimal sweep and the headline match).
    eff_shading = 3.0 if shading_pct is None else float(shading_pct)

    a = _assumptions(surface_tilt, surface_azimuth, shading_pct)
    annual, monthly = specific_yield_with_profile(lat, lon, tmy_df, a)
    diurnal = average_diurnal_profile(lat, lon, tmy_df, a)
    used_tilt = a.effective_tilt(lat)
    used_azimuth = a.effective_azimuth(lat)

    opt_tilt, opt_azimuth, opt_yield = _optimal_orientation(lat, lon, tmy_df, eff_shading)
    ratio = annual / opt_yield if opt_yield > 0 else 1.0

    # ---- P50/P90 from PVGIS interannual variability (best-effort) -------------
    p90: float | None = None
    note = "Interannual P90 not available (PVGIS interannual data unreachable)."
    cv = interannual_cv
    if cv is None and compute_interannual:
        try:
            cv = _fetch_interannual_cv(lat, lon, used_tilt, used_azimuth)
        except Exception:
            cv = None
    if cv is not None and cv >= 0:
        p90 = annual * (1.0 - _P90_Z * cv)
        note = (
            f"P90 from PVGIS interannual variability (SD_y/E_y = {cv * 100:.1f}%); "
            "P90 = P50 x (1 - 1.28 x CV), normal approximation."
        )

    return LocationProduction(
        latitude=lat,
        longitude=lon,
        specific_yield_kwh_kwp_yr=round(annual, 1),
        monthly_kwh_per_kwp=[round(m, 2) for m in monthly],
        surface_tilt=round(used_tilt, 1),
        surface_azimuth=round(used_azimuth, 1),
        method="pvlib_modelchain",
        shading_pct=round(eff_shading, 2),
        optimal_tilt=round(opt_tilt, 1),
        optimal_azimuth=round(opt_azimuth, 1),
        optimal_specific_yield_kwh_kwp_yr=round(opt_yield, 1),
        orientation_ratio=round(ratio, 4),
        p50_specific_yield_kwh_kwp_yr=round(annual, 1),
        p90_specific_yield_kwh_kwp_yr=None if p90 is None else round(p90, 1),
        interannual_note=note,
        diurnal_shape=[round(x, 5) for x in diurnal],
    )
