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
import pandas as pd
import pytest
import rioxarray  # noqa: F401  # registers the .rio accessor
import xarray as xr
from pyproj import CRS
from shapely.geometry import box

from solarsite.api.jobs import _enrich_sites, _sanity_notes_for_sites, _save_layer
from solarsite.core import AOI

_UTM35N = CRS.from_epsg(32635)


def _synthetic_tmy() -> pd.DataFrame:
    """A simple 8760-hour diurnal TMY (tz-aware) sufficient for ModelChain."""
    idx = pd.date_range("2005-01-01", periods=8760, freq="h", tz="UTC")
    hour = np.array([ts.hour for ts in idx], dtype=float)
    # Daylight bell 06:00-18:00, peak ~900 W/m² at noon; 0 at night.
    day = np.clip(np.sin((hour - 6.0) / 12.0 * np.pi), 0.0, None)
    ghi = 900.0 * day
    return pd.DataFrame(
        {
            "ghi": ghi,
            "dni": 700.0 * day,
            "dhi": 200.0 * day,
            "temp_air": np.full(8760, 25.0),
            "wind_speed": np.full(8760, 2.0),
        },
        index=idx,
    )


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


def _aoi() -> AOI:
    return AOI.from_geojson(
        {"type": "Polygon", "coordinates": [[[27, 31], [28, 31], [28, 31.5], [27, 31.5], [27, 31]]]}
    )


def test_enrich_sites_offline_labels_method() -> None:
    """Without a TMY, the offline GHI*PR path is used AND labelled (E3)."""
    out = _enrich_sites(_sites_gdf(), _aoi(), {"ghi_annual": _ghi_layer()})
    assert (out["energy_method"] == "ghi_pr_offline").all()


def test_enrich_sites_modelchain_is_default_when_tmy_present() -> None:
    """With a TMY available, displayed numbers come from pvlib ModelChain (E3)."""
    sites = _sites_gdf()
    sites["centroid_lat"] = [31.2, 31.1]
    sites["centroid_lon"] = [27.4, 27.6]
    out = _enrich_sites(sites, _aoi(), {"ghi_annual": _ghi_layer()}, tmy_df=_synthetic_tmy())
    assert (out["energy_method"] == "pvlib_modelchain").all()
    sy = out["kwh_per_kwp_yr"].to_numpy(dtype=float)
    assert np.all(np.isfinite(sy)) and np.all(sy > 0)


def test_sanity_notes_empty_for_good_sites() -> None:
    """A realistic offline result produces no sanity violations."""
    out = _enrich_sites(_sites_gdf(), _aoi(), {"ghi_annual": _ghi_layer()})
    assert _sanity_notes_for_sites(out) == []


def test_sanity_notes_flag_impossible_specific_yield() -> None:
    """An out-of-envelope displayed number is surfaced as a caveat, not hidden."""
    out = _enrich_sites(_sites_gdf(), _aoi(), {"ghi_annual": _ghi_layer()})
    out.loc[out.index[0], "kwh_per_kwp_yr"] = 9999.0  # impossible
    notes = _sanity_notes_for_sites(out)
    assert any("outside physical envelope" in n for n in notes)


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
