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
