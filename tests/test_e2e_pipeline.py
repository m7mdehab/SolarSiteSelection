"""Phase-2 gate: synthetic end-to-end pipeline test.

Chains every analysis stage on tiny synthetic inputs, fully offline (CI-safe):

    suitability layers -> weighted_overlay (LSI) -> classify_lsi (5 classes)
        -> extract_sites (candidate polygons) -> site_energy (pvlib yield + LCOE)

No network, no cache, no real datasets — just the math, wired together. This is
the integration anchor that proves the pieces compose into a working pipeline.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import rioxarray  # noqa: F401  # registers the .rio accessor
import xarray as xr
from pyproj import CRS

from solarsite.analysis import (
    EnergyAssumptions,
    classify_lsi,
    extract_sites,
    load_registry,
    site_energy,
    weighted_overlay,
)
from solarsite.core import GridSpec, empty_dataarray

_UTM36N = CRS.from_epsg(32636)


def _suitability_layer(spec: GridSpec, blob: float, background: float) -> xr.DataArray:
    """A 0..1 suitability raster: a central high-value blob over a low background."""
    da = empty_dataarray(spec, name="s", fill_value=background)
    h, w = da.shape
    # central blob covering rows/cols [h/3, 2h/3) -> a contiguous high-suitability patch
    da.values[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3] = blob
    return da


def _synthetic_tmy_year() -> pd.DataFrame:
    """A deterministic 8760-hour TMY with a diurnal GHI cycle (pvlib-ready)."""
    idx = pd.date_range("2023-01-01", periods=8760, freq="h", tz="UTC")
    hour = idx.to_series().dt.hour.to_numpy()
    # Daytime half-sine GHI peaking ~900 W/m² at noon; zero at night.
    day = np.clip(np.sin((hour - 6) / 12 * np.pi), 0, None)
    ghi = 900.0 * day
    return pd.DataFrame(
        {
            "ghi": ghi,
            "dni": 0.85 * ghi,
            "dhi": 0.15 * ghi,
            "temp_air": 20.0 + 5.0 * day,
            "wind_speed": np.full(ghi.shape, 4.0),
        },
        index=idx,
    )


def test_synthetic_end_to_end_fixtures_to_lsi_to_sites_to_energy() -> None:
    # 30x30 grid at 100 m => 3 km x 3 km; central blob ~ 1 km² (>= 0.5 km² site min).
    spec = GridSpec(minx=0.0, miny=0.0, maxx=3000.0, maxy=3000.0, resolution_m=100, crs=_UTM36N)
    registry = load_registry()

    # --- suitability layers (pre-reclassified 0..1) for a spread of factor criteria
    keys = ["solar_radiation", "slope", "dist_ptl", "lulc"]
    layers = {k: _suitability_layer(spec, blob=0.95, background=0.1) for k in keys}

    # --- weighted overlay -> continuous LSI
    lsi = weighted_overlay(layers, registry)
    assert lsi.shape == (spec.height, spec.width)
    assert float(np.nanmin(lsi.values)) >= 0.0
    assert float(np.nanmax(lsi.values)) <= 1.0
    # the blob must score strictly higher than the background
    assert float(np.nanmax(lsi.values)) > float(np.nanmin(lsi.values)) + 0.5

    # --- classify into 5 classes
    classes = classify_lsi(lsi, n_classes=5, method="equal_interval")
    assert set(np.unique(classes.values)).issubset({-9999, 1, 2, 3, 4, 5})
    assert (classes.values == 5).any()  # the blob lands in the top class

    # --- extract candidate sites from the top class
    sites = extract_sites(lsi, classes, top_classes=(5,), min_area_km2=0.5, top_k=5)
    assert len(sites) >= 1
    top = sites.iloc[0]
    assert top["rank"] == 1
    assert top["area_km2"] >= 0.5
    assert 0.0 <= top["mean_lsi"] <= 1.0

    # --- energy + economics for the top site
    tmy = _synthetic_tmy_year()
    result = site_energy(
        lat=31.0,
        lon=27.0,
        area_km2=float(top["area_km2"]),
        tmy_df=tmy,
        assumptions=EnergyAssumptions(),
    )
    assert result.specific_yield_kwh_kwp_yr > 0.0
    assert result.capacity_mwp == pytest.approx(top["area_km2"] * 45.0, rel=1e-6)
    assert result.annual_gwh > 0.0
    assert result.lcoe_usd_per_mwh > 0.0
