"""Tests for src/solarsite/acquire/landcover.py (P1.4).

CI must NOT hit the network.  All WorldCover fetches are intercepted with
httpx.MockTransport (tiny synthetic GeoTIFFs < 100 KB).  WDPA tests use
synthetic GeoPackages in tmp_path.  A single ``@pytest.mark.live`` test hits
real AWS and is excluded from CI.
"""

from __future__ import annotations

import io
import json
import warnings
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import httpx
import numpy as np
import pytest
import rasterio
import xarray as xr
from rasterio.transform import from_bounds
from shapely.geometry import box

from solarsite.acquire.landcover import (
    WDPASource,
    WorldCoverSource,
    _covering_tiles,
    _wc_tile_name,
    exclusion_mask,
)
from solarsite.core import AOI, DiskCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "nw_coast_aoi.geojson"


def _aoi() -> AOI:
    return AOI.from_geojson(json.loads(_FIXTURE.read_text()))


def _small_aoi() -> AOI:
    """A 0.3 x 0.3 degree AOI centred in the NW-coast area -- single tile, fast."""
    return AOI.from_geojson(
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [27.0, 31.0],
                        [27.3, 31.0],
                        [27.3, 31.3],
                        [27.0, 31.3],
                        [27.0, 31.0],
                    ]
                ],
            },
        }
    )


def _make_synthetic_worldcover_tif(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    width: int = 30,
    height: int = 30,
    fill_code: int = 60,
    patch_code: int = 80,
    patch_row: int = 5,
    patch_rows: int = 5,
) -> bytes:
    """Create a tiny in-memory GeoTIFF with WorldCover class codes.

    Most pixels are *fill_code* (default 60 = Bare/sparse).  Rows
    ``patch_row`` through ``patch_row + patch_rows`` are set to *patch_code*
    (default 80 = water) so resampling tests can verify nearest-neighbour
    preservation.
    """
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    data = np.full((height, width), fill_code, dtype=np.uint8)
    data[patch_row : patch_row + patch_rows, :] = patch_code

    buf = io.BytesIO()
    with rasterio.open(
        buf,
        "w",
        driver="GTiff",
        count=1,
        dtype=np.uint8,
        crs="EPSG:4326",
        transform=transform,
        width=width,
        height=height,
        nodata=0,
    ) as ds:
        ds.write(data, 1)
    return buf.getvalue()


def _make_worldcover_transport(
    aoi: AOI, fill_code: int = 60, patch_code: int = 80
) -> httpx.MockTransport:
    """Return an httpx.MockTransport that serves synthetic WorldCover tiles.

    Each tile spans the full 3x3-degree tile extent (30x30 pixels).  The
    small_aoi (27.0-27.3E, 31.0-31.3N) sits in tile N30E027 (lat 30-33).
    At 0.1 deg/row (30 rows over 3 deg), rows 17-18 cover lat 31.3-31.1
    (inside the AOI) and row 19 covers 31.1-31.0.  So:
      - patch_row=17, patch_rows=2  -> patch covers ~2/3 of the AOI height
      - fill covers the remaining ~1/3
    Both codes appear in the reprojected output.
    """
    minx, miny, maxx, maxy = aoi.bounds

    tiles = _covering_tiles(minx, miny, maxx, maxy)
    tif_bytes: dict[str, bytes] = {}
    for lat, lon in tiles:
        name = _wc_tile_name(lat, lon)
        tif_bytes[name] = _make_synthetic_worldcover_tif(
            lon,
            lat,
            lon + 3,
            lat + 3,
            width=30,
            height=30,
            fill_code=fill_code,
            patch_code=patch_code,
            patch_row=17,
            patch_rows=2,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for name, data in tif_bytes.items():
            if name in url:
                return httpx.Response(200, content=data)
        return httpx.Response(404, content=b"not found")

    return httpx.MockTransport(handler)


_REAL_HTTPX_CLIENT = httpx.Client  # captured before any patching


def _mock_client(transport: httpx.MockTransport) -> httpx.Client:
    """Build a Client wired to a MockTransport; ignores keyword args from the source."""
    return _REAL_HTTPX_CLIENT(transport=transport)


# ---------------------------------------------------------------------------
# Tile-naming unit tests
# ---------------------------------------------------------------------------


class TestTileHelpers:
    def test_wc_tile_name_positive(self) -> None:
        assert _wc_tile_name(30, 27) == "N30E027"

    def test_wc_tile_name_south_west(self) -> None:
        assert _wc_tile_name(-33, -70) == "S33W070"

    def test_wc_tile_name_zero_lat(self) -> None:
        assert _wc_tile_name(0, 36) == "N00E036"

    def test_covering_tiles_single(self) -> None:
        # 27.0 to 27.3 E, 31.0 to 31.3 N sits entirely in the N30E027 tile.
        tiles = _covering_tiles(27.0, 31.0, 27.3, 31.3)
        assert (30, 27) in tiles
        assert len(tiles) == 1

    def test_covering_tiles_cross_boundary(self) -> None:
        # Straddles the 30N tile boundary: tiles N27 and N30.
        tiles = _covering_tiles(27.0, 29.0, 27.3, 31.0)
        lats = {t[0] for t in tiles}
        assert 27 in lats
        assert 30 in lats

    def test_covering_tiles_four_tiles(self) -> None:
        # Straddles both lat and lon tile boundaries.
        tiles = _covering_tiles(29.5, 29.5, 30.5, 30.5)
        assert len(tiles) == 4


# ---------------------------------------------------------------------------
# WorldCoverSource -- mocked fetch
# ---------------------------------------------------------------------------


class TestWorldCoverSourceMocked:
    def test_returns_integer_dataarray(self, tmp_path: Path) -> None:
        """fetch() must return uint8 DataArray (integer codes, not floats)."""
        aoi = _small_aoi()
        transport = _make_worldcover_transport(aoi)
        cache = DiskCache(tmp_path)
        src = WorldCoverSource(cache=cache)

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            da = src._fetch_uncached(aoi, resolution_m=100)

        assert isinstance(da, xr.DataArray)
        assert np.issubdtype(da.dtype, np.integer), f"Expected integer dtype, got {da.dtype}"

    def test_codes_in_expected_range(self, tmp_path: Path) -> None:
        """All emitted codes must be valid WorldCover v2 codes or 0 (nodata)."""
        valid_codes = {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100}
        aoi = _small_aoi()
        transport = _make_worldcover_transport(aoi, fill_code=60, patch_code=80)
        cache = DiskCache(tmp_path)
        src = WorldCoverSource(cache=cache)

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            da = src._fetch_uncached(aoi, resolution_m=100)

        unique = set(np.unique(da.values).tolist())
        assert unique <= valid_codes, f"Unexpected codes: {unique - valid_codes}"

    def test_nearest_neighbour_preserves_patch_code(self, tmp_path: Path) -> None:
        """A patch of code 80 in the source must appear as 80 in the output.

        Nearest-neighbour never averages categorical codes.
        """
        aoi = _small_aoi()
        # fill=60, patch=80 occupies rows 5 to 9 (5 out of 30) in each tile
        transport = _make_worldcover_transport(aoi, fill_code=60, patch_code=80)
        cache = DiskCache(tmp_path)
        src = WorldCoverSource(cache=cache)

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            da = src._fetch_uncached(aoi, resolution_m=100)

        values = da.values.astype(int)
        assert 80 in values, "Code 80 should survive nearest-neighbour resampling."
        assert 60 in values, "Fill code 60 should also be present."
        # No averaged/interpolated codes (e.g. 70 between 60 and 80).
        unique = set(np.unique(values).tolist())
        assert unique <= {0, 60, 80}, f"Unexpected codes imply averaging: {unique}"

    def test_aligned_to_grid(self, tmp_path: Path) -> None:
        """Output DataArray must match the GridSpec dimensions and CRS."""
        from solarsite.acquire.base import grid_for_aoi

        aoi = _small_aoi()
        grid = grid_for_aoi(aoi, resolution_m=100)
        transport = _make_worldcover_transport(aoi)
        cache = DiskCache(tmp_path)
        src = WorldCoverSource(cache=cache)

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            da = src._fetch_uncached(aoi, resolution_m=100)

        assert da.shape == (grid.height, grid.width)
        assert da.rio.crs == grid.crs

    def test_cache_hit(self, tmp_path: Path) -> None:
        """Second fetch with same params must hit the cache (get_or_compute called once
        with compute_fn, then once without)."""
        aoi = _small_aoi()
        transport = _make_worldcover_transport(aoi)
        src = WorldCoverSource(cache=DiskCache(tmp_path))

        compute_count = {"n": 0}
        stored: dict[str, xr.DataArray] = {}

        # Replace the cache with a spy that records compute_fn calls.
        class SpyCache:
            def get_or_compute(self, source: str, aoi_hash: str, params: dict, compute_fn):  # type: ignore[no-untyped-def]
                key = f"{source}|{aoi_hash}|{params}"
                if key not in stored:
                    compute_count["n"] += 1
                    stored[key] = compute_fn()
                return stored[key]

        src._cache = SpyCache()  # type: ignore[assignment]

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            da1 = src.fetch(aoi, resolution_m=100)
            n_after_first = compute_count["n"]
            da2 = src.fetch(aoi, resolution_m=100)

        assert n_after_first == 1, "First call should compute once."
        assert compute_count["n"] == 1, "Second call must use cache (no recompute)."
        assert np.array_equal(da1.values, da2.values)

    def test_cache_miss_on_different_resolution(self, tmp_path: Path) -> None:
        """Different resolution_m -> different cache key -> compute_fn called again."""
        aoi = _small_aoi()
        transport = _make_worldcover_transport(aoi)
        src = WorldCoverSource(cache=DiskCache(tmp_path))

        compute_count = {"n": 0}
        stored: dict[str, xr.DataArray] = {}

        class SpyCache:
            def get_or_compute(self, source: str, aoi_hash: str, params: dict, compute_fn):  # type: ignore[no-untyped-def]
                key = f"{source}|{aoi_hash}|{params}"
                if key not in stored:
                    compute_count["n"] += 1
                    stored[key] = compute_fn()
                return stored[key]

        src._cache = SpyCache()  # type: ignore[assignment]

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            src.fetch(aoi, resolution_m=100)
            n1 = compute_count["n"]
            src.fetch(aoi, resolution_m=200)
            n2 = compute_count["n"]

        assert n1 == 1
        assert n2 == 2, "Different resolution must be a cache miss (new compute)."


# ---------------------------------------------------------------------------
# WDPASource tests
# ---------------------------------------------------------------------------


def _make_wdpa_gpkg(path: Path, aoi: AOI) -> None:
    """Write a tiny GeoPackage with one protected-area polygon inside the AOI."""
    minx, miny, maxx, maxy = aoi.bounds
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2
    poly = box(cx - 0.05, cy - 0.05, cx + 0.05, cy + 0.05)
    gdf = gpd.GeoDataFrame(
        {"NAME_ENG": ["Test PA"], "geometry": [poly]},
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GPKG")


class TestWDPASource:
    def test_clip_and_reproject(self, tmp_path: Path) -> None:
        """Returned GeoDataFrame must be in working CRS and intersect the AOI."""
        aoi = _small_aoi()
        gpkg = tmp_path / "wdpa.gpkg"
        _make_wdpa_gpkg(gpkg, aoi)

        cache = DiskCache(tmp_path / "cache")
        src = WDPASource(wdpa_path=gpkg, cache=cache)
        assert src.available is True

        gdf = src.fetch(aoi, resolution_m=100)

        from solarsite.core import working_crs_for

        expected_crs = working_crs_for(aoi.geometry)
        assert gdf.crs == expected_crs
        assert len(gdf) == 1

    def test_absent_file_returns_empty_geodataframe(self, tmp_path: Path) -> None:
        """If the WDPA file doesn't exist: available=False, empty GDF, no crash."""
        aoi = _small_aoi()
        missing = tmp_path / "no_such_file.gpkg"

        with pytest.warns(UserWarning, match="WDPA file not found"):
            src = WDPASource(wdpa_path=missing)

        assert src.available is False

        cache = DiskCache(tmp_path / "cache")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            src2 = WDPASource(wdpa_path=missing, cache=cache)

        gdf = src2._fetch_uncached(aoi, resolution_m=100)

        from solarsite.core import working_crs_for

        expected_crs = working_crs_for(aoi.geometry)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert gdf.empty
        assert gdf.crs == expected_crs

    def test_absent_file_no_raise(self, tmp_path: Path) -> None:
        """_fetch_uncached must not raise when the WDPA file is absent."""
        aoi = _small_aoi()
        missing = tmp_path / "gone.gpkg"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            src = WDPASource(wdpa_path=missing, cache=DiskCache(tmp_path / "cache"))
            # Must not raise.
            src._fetch_uncached(aoi, resolution_m=100)

    def test_polygon_outside_aoi_excluded(self, tmp_path: Path) -> None:
        """Polygons that don't intersect the AOI must be clipped out."""
        aoi = _small_aoi()
        # Create a protected area far outside the AOI.
        poly_outside = box(0.0, 0.0, 0.5, 0.5)  # somewhere in the Gulf of Guinea
        gdf_raw = gpd.GeoDataFrame(
            {"NAME_ENG": ["Far Away PA"], "geometry": [poly_outside]},
            crs="EPSG:4326",
        )
        gpkg = tmp_path / "wdpa_outside.gpkg"
        gdf_raw.to_file(gpkg, driver="GPKG")

        src = WDPASource(wdpa_path=gpkg, cache=DiskCache(tmp_path / "cache"))
        gdf = src._fetch_uncached(aoi, resolution_m=100)
        assert gdf.empty


class _SpyCache:
    """In-memory stub for DiskCache -- stores results without writing to disk.

    Avoids the xr.open_dataarray multi-variable bug that occurs when rioxarray
    DataArrays (which carry a spatial_ref variable) are round-tripped through
    the NetCDF-based DiskCache.
    """

    def __init__(self) -> None:
        self._store: dict[str, xr.DataArray | gpd.GeoDataFrame] = {}
        self.compute_calls = 0

    def get_or_compute(self, source: str, aoi_hash: str, params: dict, compute_fn):  # type: ignore[no-untyped-def]
        key = f"{source}|{aoi_hash}|{params}"
        if key not in self._store:
            self.compute_calls += 1
            self._store[key] = compute_fn()
        return self._store[key]


# ---------------------------------------------------------------------------
# exclusion_mask tests
# ---------------------------------------------------------------------------


class TestExclusionMask:
    def _make_worldcover_source_with_patch(
        self, aoi: AOI, patch_code: int = 80
    ) -> WorldCoverSource:
        """Return a WorldCoverSource backed by a SpyCache pre-warmed with synthetic data."""
        transport = _make_worldcover_transport(aoi, fill_code=60, patch_code=patch_code)
        spy = _SpyCache()
        src = WorldCoverSource(cache=DiskCache())  # cache arg required by __init__
        src._cache = spy  # type: ignore[assignment]

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            src.fetch(aoi, resolution_m=100)  # warm SpyCache
        return src

    def _make_absent_wdpa(self, tmp_path: Path) -> WDPASource:
        """Return a WDPASource pointing to a non-existent file (SpyCache)."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            src = WDPASource(wdpa_path=tmp_path / "no_wdpa.gpkg")
        src._cache = _SpyCache()  # type: ignore[assignment]
        return src

    def test_water_class_excluded(self, tmp_path: Path) -> None:
        """Cells with class 80 (water) must be True in the exclusion mask."""
        aoi = _small_aoi()
        wc_src = self._make_worldcover_source_with_patch(aoi, patch_code=80)
        wdpa_src = self._make_absent_wdpa(tmp_path)

        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        assert mask.values.max() == 1, "At least some cells should be excluded."
        # Cells that were code 60 (non-excluded class) must be 0.
        assert mask.values.min() == 0, "Some cells should be non-excluded."

    def test_buildup_class_excluded(self, tmp_path: Path) -> None:
        """Cells with class 50 (built-up) must be True in the exclusion mask."""
        aoi = _small_aoi()
        wc_src = self._make_worldcover_source_with_patch(aoi, patch_code=50)
        wdpa_src = self._make_absent_wdpa(tmp_path)

        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        assert mask.values.max() == 1

    def test_wdpa_polygon_excluded(self, tmp_path: Path) -> None:
        """Cells under a WDPA polygon must be True even if LULC is not excluded."""
        aoi = _small_aoi()
        # All cells are class 60 (bare/suitable -- no LULC exclusion).
        transport = _make_worldcover_transport(aoi, fill_code=60, patch_code=60)
        wc_src = WorldCoverSource(cache=DiskCache())
        spy = _SpyCache()
        wc_src._cache = spy  # type: ignore[assignment]

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            wc_src.fetch(aoi, resolution_m=100)

        gpkg = tmp_path / "wdpa_poly.gpkg"
        _make_wdpa_gpkg(gpkg, aoi)
        wdpa_src = WDPASource(wdpa_path=gpkg, cache=DiskCache(tmp_path / "cache_wdpa"))
        wdpa_spy = _SpyCache()
        wdpa_src._cache = wdpa_spy  # type: ignore[assignment]

        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        # There must be at least one excluded cell from the WDPA polygon.
        assert mask.values.max() == 1, "WDPA polygon cells must be excluded."

    def test_all_suitable_when_no_exclusions(self, tmp_path: Path) -> None:
        """When LULC is all class 60 and no WDPA -> mask is all-zero."""
        aoi = _small_aoi()
        transport = _make_worldcover_transport(aoi, fill_code=60, patch_code=60)
        wc_src = WorldCoverSource(cache=DiskCache())
        wc_src._cache = _SpyCache()  # type: ignore[assignment]

        with patch(
            "solarsite.acquire.landcover.httpx.Client",
            side_effect=lambda **kw: _mock_client(transport),
        ):
            wc_src.fetch(aoi, resolution_m=100)

        wdpa_src = self._make_absent_wdpa(tmp_path)

        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        assert mask.values.max() == 0, "All cells should be non-excluded."

    def test_mask_aligned_to_grid(self, tmp_path: Path) -> None:
        """Exclusion mask must have correct shape and CRS."""
        from solarsite.acquire.base import grid_for_aoi

        aoi = _small_aoi()
        wc_src = self._make_worldcover_source_with_patch(aoi, patch_code=80)
        wdpa_src = self._make_absent_wdpa(tmp_path)

        grid = grid_for_aoi(aoi, resolution_m=100)
        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        assert mask.shape == (grid.height, grid.width)
        assert mask.rio.crs == grid.crs

    def test_combined_water_plus_wdpa(self, tmp_path: Path) -> None:
        """Combined exclusion: water cells (80) + WDPA polygon -> max=1."""
        aoi = _small_aoi()
        wc_src = self._make_worldcover_source_with_patch(aoi, patch_code=80)
        gpkg = tmp_path / "wdpa_combined.gpkg"
        _make_wdpa_gpkg(gpkg, aoi)
        wdpa_src = WDPASource(wdpa_path=gpkg, cache=DiskCache(tmp_path / "cache_wdpa"))
        wdpa_src._cache = _SpyCache()  # type: ignore[assignment]

        mask = exclusion_mask(aoi, resolution_m=100, worldcover_source=wc_src, wdpa_source=wdpa_src)
        assert mask.values.max() == 1


# ---------------------------------------------------------------------------
# Live test -- real AWS, excluded from CI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Edge-case coverage tests
# ---------------------------------------------------------------------------


class TestWorldCoverEdgeCases:
    def test_404_tile_skipped_and_all_missing_raises(self, tmp_path: Path) -> None:
        """If every tile returns 404, AcquisitionError must be raised."""
        from solarsite.acquire.landcover import AcquisitionError

        aoi = _small_aoi()

        def all_404(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"not found")

        src = WorldCoverSource(cache=DiskCache(tmp_path))

        with (
            pytest.raises(AcquisitionError, match="missing"),
            patch(
                "solarsite.acquire.landcover.httpx.Client",
                side_effect=lambda **kw: _mock_client(httpx.MockTransport(all_404)),
            ),
        ):
            src._fetch_uncached(aoi, resolution_m=100)

    def test_non_200_non_404_raises(self, tmp_path: Path) -> None:
        """HTTP 500 must raise AcquisitionError."""
        from solarsite.acquire.landcover import AcquisitionError

        aoi = _small_aoi()

        def server_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"err")

        src = WorldCoverSource(cache=DiskCache(tmp_path))

        with (
            pytest.raises(AcquisitionError),
            patch(
                "solarsite.acquire.landcover.httpx.Client",
                side_effect=lambda **kw: _mock_client(httpx.MockTransport(server_error)),
            ),
        ):
            src._fetch_uncached(aoi, resolution_m=100)


class TestWDPASourceNonWGS84(TestWDPASource):
    def test_wdpa_non_wgs84_source_reprojected_before_clip(self, tmp_path: Path) -> None:
        """If the WDPA file is NOT in WGS-84, it should be re-projected before the clip."""
        aoi = _small_aoi()
        # Create a GeoPackage in UTM CRS (not WGS-84).
        minx, miny, maxx, maxy = aoi.bounds
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
        poly_wgs84 = box(cx - 0.05, cy - 0.05, cx + 0.05, cy + 0.05)
        gdf_wgs84 = gpd.GeoDataFrame({"NAME_ENG": ["PA"]}, geometry=[poly_wgs84], crs="EPSG:4326")
        # Reproject to UTM 35N before saving.
        gdf_utm = gdf_wgs84.to_crs("EPSG:32635")
        gpkg = tmp_path / "wdpa_utm.gpkg"
        gdf_utm.to_file(gpkg, driver="GPKG")

        from solarsite.core import working_crs_for

        src = WDPASource(wdpa_path=gpkg, cache=DiskCache(tmp_path / "cache"))
        gdf = src._fetch_uncached(aoi, resolution_m=100)
        expected_crs = working_crs_for(aoi.geometry)
        assert gdf.crs == expected_crs
        assert len(gdf) == 1


@pytest.mark.live
def test_live_worldcover_real_fetch() -> None:
    """Fetch a real WorldCover tile from AWS and verify integer class codes.

    Uses a tiny 0.1 x 0.1 degree sub-bbox of the NW-coast AOI to minimise data.
    Skipped unless ``pytest -m live`` is passed explicitly.
    """
    # A tiny sub-bbox well within the N30E027 tile.
    tiny_aoi = AOI.from_geojson(
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [27.0, 31.0],
                        [27.1, 31.0],
                        [27.1, 31.1],
                        [27.0, 31.1],
                        [27.0, 31.0],
                    ]
                ],
            },
        }
    )

    src = WorldCoverSource()
    da = src._fetch_uncached(tiny_aoi, resolution_m=100)

    assert isinstance(da, xr.DataArray)
    assert np.issubdtype(da.dtype, np.integer), f"Expected integer codes, got {da.dtype}"

    valid_codes = {0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100}
    unique = set(np.unique(da.values).tolist())
    assert unique <= valid_codes, f"Unexpected codes from real tile: {unique - valid_codes}"
    assert len(unique) >= 1, "Should have at least one class code."
    print(f"Live test: unique codes = {sorted(unique)}, shape = {da.shape}")
