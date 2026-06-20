"""Validation-grade per-location production for the consumer mode (Step 2/3.1).

Fetches the PVGIS Typical Meteorological Year for the user's location (cached via
the standard DiskCache) and runs the pvlib ModelChain to get a validation-grade
annual specific yield plus its monthly profile. This is the SAME physics validated
against the PVGIS oracle to <=2.2% (see tests/test_oracle_reference.py), so the
production numbers a consumer sees are validation-grade — not the cruder GHI*PR
offline estimate.

Network: this calls PVGIS. It is therefore exercised only by a live-marked test;
unit tests inject a TMY DataFrame instead. In the deployed Space PVGIS is reachable.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from solarsite.analysis.energy import EnergyAssumptions, specific_yield_with_profile

__all__ = ["LocationProduction", "location_production"]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class LocationProduction(BaseModel):
    """Validation-grade specific yield + monthly profile for one location."""

    latitude: float
    longitude: float
    specific_yield_kwh_kwp_yr: float
    monthly_kwh_per_kwp: list[float] = Field(..., description="12 values, Jan..Dec; sum=annual.")
    month_labels: list[str] = Field(default_factory=lambda: list(_MONTHS))
    surface_tilt: float
    surface_azimuth: float
    method: str = "pvlib_modelchain"


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


def location_production(
    lat: float,
    lon: float,
    assumptions: EnergyAssumptions | None = None,
    *,
    tmy_df: pd.DataFrame | None = None,
    cache: Any | None = None,
) -> LocationProduction:
    """Compute validation-grade specific yield + monthly profile for a location.

    If ``tmy_df`` is supplied (tests), it is used directly; otherwise the PVGIS TMY
    for the point is fetched (cached). Raises on network failure — we never invent
    a production number when the physics can't be computed.
    """
    a = assumptions or EnergyAssumptions()
    if tmy_df is None:
        from solarsite.acquire.pvgis import PVGISSource

        tmy_df = PVGISSource(cache=cache).fetch_tmy(_tiny_aoi(lat, lon))

    annual, monthly = specific_yield_with_profile(lat, lon, tmy_df, a)
    return LocationProduction(
        latitude=lat,
        longitude=lon,
        specific_yield_kwh_kwp_yr=round(annual, 1),
        monthly_kwh_per_kwp=[round(m, 2) for m in monthly],
        surface_tilt=a.effective_tilt(lat),
        surface_azimuth=a.effective_azimuth(lat),
        method="pvlib_modelchain",
    )
