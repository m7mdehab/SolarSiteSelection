"""Regression tests for the analysis-job pipeline (src/solarsite/api/jobs.py).

These guard the two bugs that white-screened the deployed app:
1. Candidate sites lacked energy fields (`_enrich_sites` was a stub) → the
   frontend called `.toFixed()` on undefined → crash.
2. The LSI layer failed to save: the scipy netcdf backend can't write the WKT
   `crs` string attr (`KeyError: ('U', 60)`), so the headline output was lost
   while the job still reported "done".
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rioxarray  # noqa: F401  # registers the .rio accessor
import xarray as xr
from pyproj import CRS
from shapely.geometry import box

from solarsite.api.jobs import _enrich_sites, _save_layer
from solarsite.core import AOI

_UTM35N = CRS.from_epsg(32635)


def _ghi_layer() -> xr.DataArray:
    """Synthetic annual-GHI raster (kWh/m²/yr) on a small working-CRS grid."""
    x = np.array([0.0, 500.0, 1000.0, 1500.0])
    y = np.array([1500.0, 1000.0, 500.0, 0.0])
    data = np.full((4, 4), 2100.0)
    da = xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x}, name="ghi_annual")
    return da.rio.write_crs(_UTM35N)


def _sites_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "site_id": [1, 2],
            "area_km2": [1.5, 0.8],
            "mean_lsi": [0.9, 0.7],
            "centroid_x": [500.0, 1000.0],
            "centroid_y": [1000.0, 500.0],
            "rank": [1, 2],
        },
        geometry=[box(0, 0, 100, 100), box(200, 0, 300, 100)],
        crs=_UTM35N,
    )


def test_enrich_sites_adds_finite_energy_fields() -> None:
    """Every site must carry kwh/gwh/lcoe/capacity as finite numbers (no undefined)."""
    aoi = AOI.from_geojson(
        {"type": "Polygon", "coordinates": [[[27, 31], [28, 31], [28, 31.5], [27, 31.5], [27, 31]]]}
    )
    out = _enrich_sites(_sites_gdf(), aoi, {"ghi_annual": _ghi_layer()})
    for col in ("kwh_per_kwp_yr", "gwh_per_yr", "lcoe", "capacity_mwp"):
        assert col in out.columns, f"missing energy column {col}"
        vals = out[col].to_numpy(dtype=float)
        assert np.all(np.isfinite(vals)), f"{col} has non-finite values: {vals}"
        assert np.all(vals > 0), f"{col} must be positive: {vals}"
    # GHI 2100 * PR 0.75 ≈ 1575 kWh/kWp/yr
    assert 1400 < float(out["kwh_per_kwp_yr"].iloc[0]) < 1800
    # Larger site → more capacity and generation.
    assert out["capacity_mwp"].iloc[0] > out["capacity_mwp"].iloc[1]


def test_enrich_sites_no_ghi_layer_still_populates() -> None:
    """Even without a GHI layer the fields exist (default), never undefined."""
    aoi = AOI.from_geojson(
        {"type": "Polygon", "coordinates": [[[27, 31], [28, 31], [28, 31.5], [27, 31.5], [27, 31]]]}
    )
    out = _enrich_sites(_sites_gdf(), aoi, {})
    assert np.all(np.isfinite(out["kwh_per_kwp_yr"].to_numpy(dtype=float)))


def test_save_layer_lsi_saves_and_loads_numeric(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A CRS-tagged float LSI must SAVE and reload as a numeric raster.

    Regression: the WKT crs string attr broke the scipy netcdf backend
    (KeyError ('U', 60)) and the layer was silently dropped.
    """
    lsi = xr.DataArray(
        np.linspace(0.0, 1.0, 16).reshape(4, 4).astype("float64"),
        dims=["y", "x"],
        coords={"y": [3.0, 2.0, 1.0, 0.0], "x": [0.0, 1.0, 2.0, 3.0]},
        name="lsi",
    ).rio.write_crs(_UTM35N)

    with caplog.at_level(logging.WARNING):
        _save_layer(tmp_path, "lsi", lsi)

    assert "Could not save layer" not in caplog.text, "LSI save still failing"
    out = tmp_path / "lsi.nc"
    assert out.exists(), "lsi.nc was not written"

    ds = xr.open_dataset(out)
    reloaded = ds["lsi"]
    assert reloaded.dtype.kind == "f", f"expected float raster, got {reloaded.dtype}"
    assert reloaded.shape == (4, 4)
    assert int(ds["lsi"].attrs.get("crs_epsg", 0)) == 32635
    ds.close()


def test_save_layer_class_raster_int(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The integer class raster (1-5) must also save cleanly."""
    cls = xr.DataArray(
        np.array([[1, 2], [4, 5]], dtype="int32"),
        dims=["y", "x"],
        coords={"y": [1.0, 0.0], "x": [0.0, 1.0]},
        name="class_raster",
    ).rio.write_crs(_UTM35N)
    with caplog.at_level(logging.WARNING):
        _save_layer(tmp_path, "class_raster", cls)
    assert "Could not save layer" not in caplog.text
    ds = xr.open_dataset(tmp_path / "class_raster.nc")
    assert ds["class_raster"].dtype.kind in ("i", "u")
    ds.close()
