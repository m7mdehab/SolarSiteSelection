"""Tests for the disk cache (src/solarsite/core/cache.py).

Acceptance criteria:
    - First call = cache MISS → compute_fn invoked exactly once
    - Second call = cache HIT → compute_fn NOT invoked again
    - Round-trip DataArray and GeoDataFrame through the cache and assert equality
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import xarray as xr
from pyproj import CRS
from shapely.geometry import box

from solarsite.core.cache import CacheKey, DiskCache

_UTM36N = CRS.from_epsg(32636)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dataarray() -> xr.DataArray:
    """Return a small deterministic DataArray."""
    data = np.arange(12.0).reshape(3, 4)
    da = xr.DataArray(
        data,
        dims=["y", "x"],
        coords={
            "y": [300.0, 200.0, 100.0],
            "x": [100.0, 200.0, 300.0, 400.0],
        },
        name="test_layer",
    )
    return da


def test_roundtrip_dataarray_with_crs(tmp_path: Path) -> None:
    """A rioxarray-CRS-tagged DataArray must survive a cache round-trip.

    Regression for the spatial_ref serialization bug: write_crs adds a
    spatial_ref coord that NetCDF stores as a second variable, which broke the
    old open_dataarray()-based reload. Every raster acquisition source hits this.
    """
    import rioxarray  # noqa: F401  # registers the .rio accessor

    cache = DiskCache(tmp_path)
    da = _make_dataarray().rio.write_crs(_UTM36N)
    calls = {"n": 0}

    def compute() -> xr.DataArray:
        calls["n"] += 1
        return da

    first = cache.get_or_compute("ras", "h1", {"r": 100}, compute)
    second = cache.get_or_compute("ras", "h1", {"r": 100}, compute)
    assert calls["n"] == 1  # second call is a hit
    np.testing.assert_array_equal(first.values, second.values)
    assert second.rio.crs is not None
    assert second.rio.crs.to_epsg() == 32636
    assert second.name == "test_layer"


def test_load_ignores_foreign_platform_data_path(tmp_path: Path) -> None:
    """Cache reload must not depend on the stored data_path string.

    Regression: the meta JSON recorded a platform-specific data_path (e.g.
    Windows backslashes from a Windows seed). On Linux that string did not
    resolve, so _load returned None → cache miss → the deployed demo fell
    through to live acquisition. _load must reconstruct the path from root+key.
    """
    import json

    cache = DiskCache(tmp_path)
    cache.get_or_compute("src", "hash", {"r": 100}, _make_dataarray)

    # Corrupt the stored data_path to a bogus foreign-OS string.
    meta_file = next(tmp_path.glob("*.json"))
    meta = json.loads(meta_file.read_text())
    meta["data_path"] = r"C:\seeded\on\windows\whatever.nc"
    meta_file.write_text(json.dumps(meta))

    calls = {"n": 0}

    def compute() -> xr.DataArray:
        calls["n"] += 1
        return _make_dataarray()

    # Must still HIT (reconstruct path from root+key), not recompute.
    result = cache.get_or_compute("src", "hash", {"r": 100}, compute)
    assert calls["n"] == 0, "reload wrongly depended on the stored data_path string"
    assert result is not None


def _make_geodataframe() -> gpd.GeoDataFrame:
    """Return a small deterministic GeoDataFrame."""
    polys = [box(0, 0, 1, 1), box(2, 0, 3, 1)]
    return gpd.GeoDataFrame({"value": [1.0, 2.0]}, geometry=polys, crs=_UTM36N)


# ---------------------------------------------------------------------------
# CacheKey
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic() -> None:
    k1 = CacheKey("dem", "abc123", {"resolution_m": 100})
    k2 = CacheKey("dem", "abc123", {"resolution_m": 100})
    assert k1.key_str == k2.key_str


def test_cache_key_differs_for_different_params() -> None:
    k1 = CacheKey("dem", "abc123", {"resolution_m": 100})
    k2 = CacheKey("dem", "abc123", {"resolution_m": 200})
    assert k1.key_str != k2.key_str


def test_cache_key_differs_for_different_source() -> None:
    k1 = CacheKey("dem", "abc123", {})
    k2 = CacheKey("slope", "abc123", {})
    assert k1.key_str != k2.key_str


def test_cache_key_differs_for_different_aoi_hash() -> None:
    k1 = CacheKey("dem", "abc123", {})
    k2 = CacheKey("dem", "xyz789", {})
    assert k1.key_str != k2.key_str


# ---------------------------------------------------------------------------
# Hit / Miss behaviour
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_compute_fn(tmp_path: Path) -> None:
    cache = DiskCache(root=tmp_path)
    call_count = {"n": 0}

    def compute() -> xr.DataArray:
        call_count["n"] += 1
        return _make_dataarray()

    cache.get_or_compute("dem", "h1", {"res": 100}, compute)
    assert call_count["n"] == 1


def test_cache_hit_does_not_invoke_compute_fn(tmp_path: Path) -> None:
    cache = DiskCache(root=tmp_path)
    call_count = {"n": 0}

    def compute() -> xr.DataArray:
        call_count["n"] += 1
        return _make_dataarray()

    # First call: miss
    cache.get_or_compute("dem", "h1", {"res": 100}, compute)
    assert call_count["n"] == 1

    # Second call: hit — compute should NOT be called again
    cache.get_or_compute("dem", "h1", {"res": 100}, compute)
    assert call_count["n"] == 1  # Still 1!


def test_cache_exists_reflects_hit(tmp_path: Path) -> None:
    cache = DiskCache(root=tmp_path)
    assert not cache.exists("dem", "h1", {})
    cache.get_or_compute("dem", "h1", {}, _make_dataarray)
    assert cache.exists("dem", "h1", {})


# ---------------------------------------------------------------------------
# DataArray round-trip
# ---------------------------------------------------------------------------


def test_dataarray_roundtrip(tmp_path: Path) -> None:
    original = _make_dataarray()
    cache = DiskCache(root=tmp_path)
    cache.get_or_compute("layer", "h1", {}, lambda: original)

    # Second call loads from disk
    loaded = cache.get_or_compute("layer", "h1", {}, lambda: _make_dataarray())

    assert isinstance(loaded, xr.DataArray)
    np.testing.assert_array_equal(loaded.values, original.values)
    assert list(loaded.dims) == list(original.dims)


def test_dataarray_name_preserved(tmp_path: Path) -> None:
    original = _make_dataarray()
    cache = DiskCache(root=tmp_path)
    cache.get_or_compute("layer", "h1", {}, lambda: original)
    loaded = cache.get_or_compute("layer", "h1", {}, lambda: original)
    assert loaded.name == original.name


# ---------------------------------------------------------------------------
# GeoDataFrame round-trip
# ---------------------------------------------------------------------------


def test_geodataframe_roundtrip(tmp_path: Path) -> None:
    original = _make_geodataframe()
    cache = DiskCache(root=tmp_path)
    cache.get_or_compute("wdpa", "h2", {}, lambda: original)

    loaded = cache.get_or_compute("wdpa", "h2", {}, lambda: _make_geodataframe())

    assert isinstance(loaded, gpd.GeoDataFrame)
    assert len(loaded) == len(original)
    np.testing.assert_array_almost_equal(loaded["value"].values, original["value"].values)


def test_geodataframe_geometry_preserved(tmp_path: Path) -> None:
    original = _make_geodataframe()
    cache = DiskCache(root=tmp_path)
    cache.get_or_compute("wdpa", "h3", {}, lambda: original)
    loaded = cache.get_or_compute("wdpa", "h3", {}, lambda: original)

    for orig_geom, loaded_geom in zip(original.geometry, loaded.geometry, strict=False):
        assert orig_geom.equals(loaded_geom), f"{orig_geom} != {loaded_geom}"


def test_geodataframe_crs_preserved(tmp_path: Path) -> None:
    original = _make_geodataframe()
    cache = DiskCache(root=tmp_path)
    cache.get_or_compute("wdpa", "h4", {}, lambda: original)
    loaded = cache.get_or_compute("wdpa", "h4", {}, lambda: original)
    assert loaded.crs == original.crs


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_invalidate_clears_cache(tmp_path: Path) -> None:
    cache = DiskCache(root=tmp_path)
    call_count = {"n": 0}

    def compute() -> xr.DataArray:
        call_count["n"] += 1
        return _make_dataarray()

    cache.get_or_compute("dem", "h1", {}, compute)
    assert call_count["n"] == 1

    cache.invalidate("dem", "h1", {})
    assert not cache.exists("dem", "h1", {})

    cache.get_or_compute("dem", "h1", {}, compute)
    assert call_count["n"] == 2  # Had to recompute


# ---------------------------------------------------------------------------
# Multiple independent keys don't collide
# ---------------------------------------------------------------------------


def test_different_keys_independent(tmp_path: Path) -> None:
    cache = DiskCache(root=tmp_path)
    da1 = _make_dataarray()
    da2 = xr.DataArray(np.zeros((3, 4)), dims=["y", "x"], name="zeros")

    cache.get_or_compute("layer_a", "h1", {}, lambda: da1)
    cache.get_or_compute("layer_b", "h1", {}, lambda: da2)

    loaded_a = cache.get_or_compute("layer_a", "h1", {}, lambda: da1)
    loaded_b = cache.get_or_compute("layer_b", "h1", {}, lambda: da2)

    np.testing.assert_array_equal(loaded_a.values, da1.values)
    np.testing.assert_array_equal(loaded_b.values, da2.values)
