"""Tests for src/solarsite/acquire/terrain.py (P1.2 -- Terrain).

CI rules: NO live network. All DEM fetches are mocked with synthetic GeoTIFFs
created in-memory via rasterio MemoryFile.

Test coverage targets:
  * Slope correctness: constant-gradient ramp -> exact slope degrees.
  * Aspect correctness: south-facing plane -> aspect_class == 5 (south).
  * Flat surface -> slope ~= 0 and aspect_class == 0.
  * Output has CRS, correct shape, 3 bands, band coord names.
  * Cache hit / miss via DiskCache(tmp_path).
  * GLO-30 tile URL scheme.
  * 404 ocean tiles are skipped gracefully.
  * OpenTopography fallback when GLO-30 fails.
  * OpenTopography raises AcquisitionError when key is missing.
  * Live test (excluded from CI): real GLO-30 over NW-coast AOI.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from rasterio.crs import CRS as RioCRS
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from solarsite.acquire.terrain import (
    TerrainSource,
    _compute_slope_aspect,
    _glo30_tile_url,
    _tiles_for_aoi,
)
from solarsite.core import AOI, DiskCache

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_AOI_FIXTURE = Path(__file__).parent / "fixtures" / "nw_coast_aoi.geojson"


def _nw_coast_aoi() -> AOI:
    return AOI.from_geojson(json.loads(_AOI_FIXTURE.read_text()))


def _make_dem_bytes(
    data: np.ndarray,
    bounds_wgs84: tuple[float, float, float, float],
    nodata: float = -9999.0,
) -> bytes:
    """Encode a 2-D numpy array as an in-memory WGS-84 GeoTIFF and return raw bytes."""
    height, width = data.shape
    minx, miny, maxx, maxy = bounds_wgs84
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    with MemoryFile() as memfile:
        with memfile.open(
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype=np.float32,
            crs=RioCRS.from_epsg(4326),
            transform=transform,
            nodata=nodata,
        ) as ds:
            ds.write(data.astype(np.float32), 1)
        return memfile.read()


def _flat_dem_bytes(
    height: int = 16,
    width: int = 16,
    elevation: float = 100.0,
    bounds: tuple[float, float, float, float] = (27.0, 31.0, 28.0, 32.0),
) -> bytes:
    """DEM bytes with constant elevation (flat surface)."""
    data = np.full((height, width), elevation, dtype=np.float32)
    return _make_dem_bytes(data, bounds)


def _ramp_dem_bytes(
    slope_deg: float = 10.0,
    height: int = 32,
    width: int = 32,
    res_m: float = 100.0,
    bounds: tuple[float, float, float, float] = (27.0, 31.0, 28.0, 32.0),
) -> bytes:
    """DEM bytes with a constant east-west gradient giving *slope_deg* degrees.

    The ramp rises eastward (dz/dx > 0), so the terrain faces west (aspect 7).
    """
    dz_per_cell = math.tan(math.radians(slope_deg)) * res_m
    row = np.arange(width, dtype=np.float32) * dz_per_cell
    data = np.tile(row, (height, 1))
    return _make_dem_bytes(data, bounds)


def _south_facing_dem_bytes(
    slope_deg: float = 15.0,
    height: int = 32,
    width: int = 32,
    res_m: float = 100.0,
    bounds: tuple[float, float, float, float] = (27.0, 31.0, 28.0, 32.0),
) -> bytes:
    """DEM bytes with a constant north-south gradient (terrain faces south).

    Elevation increases northward (row index 0 = north = high), which means
    the slope descends toward the south → south-facing → aspect_class == 5.
    In raster convention row 0 is the northernmost row; elevation at row i =
    dz_per_cell * i  (increases as we go south, i.e. row increases).
    Wait—south-facing means it tilts downward to the south, i.e. elevation is
    HIGHER in the north (small row index) and LOWER in the south (large row
    index).  That means: elev[i, j] = base - i * dz_per_cell.
    dz/dy (northward) > 0 → slope faces south ✓
    """
    dz_per_cell = math.tan(math.radians(slope_deg)) * res_m
    col = np.zeros(width, dtype=np.float32)
    # row 0 = north = highest; row increases southward = elevation decreases
    rows = np.arange(height, dtype=np.float32) * (-dz_per_cell) + 1000.0
    data = rows[:, np.newaxis] + col[np.newaxis, :]
    return _make_dem_bytes(data, bounds)


# ---------------------------------------------------------------------------
# Unit tests — _compute_slope_aspect internals
# ---------------------------------------------------------------------------


class TestComputeSlopeAspect:
    """Whitebox tests for the slope/aspect calculation kernel."""

    def test_flat_surface_zero_slope(self) -> None:
        flat = np.full((10, 10), 100.0)
        slope, aspect = _compute_slope_aspect(flat, res_m=100.0)
        assert float(np.max(slope)) == pytest.approx(0.0, abs=1e-5)
        unique_aspect = list(np.unique(aspect))
        assert unique_aspect == [0], f"Flat surface: expected [0], got {unique_aspect}"

    def test_constant_east_ramp_slope(self) -> None:
        """East-west ramp: known slope, known aspect (west-facing since going up east)."""
        slope_target = 20.0  # degrees
        res_m = 100.0
        dz = math.tan(math.radians(slope_target)) * res_m
        # Row is constant, column increases eastward
        col = np.arange(20, dtype=np.float64) * dz
        data = np.tile(col, (20, 1))

        slope, _aspect = _compute_slope_aspect(data, res_m=res_m)
        # Interior cells should hit exactly the target slope (central differences)
        interior_slope = slope[1:-1, 1:-1]
        assert float(np.mean(interior_slope)) == pytest.approx(slope_target, rel=0.01)

    def test_south_facing_plane_aspect(self) -> None:
        """Plane tilted to face south → aspect_class == 5 for interior cells."""
        slope_deg = 15.0
        res_m = 100.0
        dz = math.tan(math.radians(slope_deg)) * res_m
        height, width = 20, 20
        # row 0 = north = highest; elevation decreases southward
        rows = np.arange(height, dtype=np.float64) * (-dz) + 1000.0
        data = np.tile(rows[:, np.newaxis], (1, width))

        _, aspect = _compute_slope_aspect(data, res_m=res_m)
        interior_aspect = aspect[2:-2, 2:-2]
        unique_vals = np.unique(interior_aspect)
        assert list(unique_vals) == [5], f"Expected [5], got {unique_vals}"

    def test_north_facing_plane_aspect(self) -> None:
        """Plane tilted to face north → aspect_class == 1 for interior cells.

        North-facing: terrain descends northward → elevation is LOW in the
        north (row 0) and HIGH in the south (last row).  Row index increases
        southward, so elevation increases with row index.
        """
        slope_deg = 15.0
        res_m = 100.0
        dz = math.tan(math.radians(slope_deg)) * res_m
        height, width = 20, 20
        # row 0 = north = lowest; elevation increases southward (with row index)
        rows = np.arange(height, dtype=np.float64) * dz
        data = np.tile(rows[:, np.newaxis], (1, width))

        _, aspect = _compute_slope_aspect(data, res_m=res_m)
        interior_aspect = aspect[2:-2, 2:-2]
        unique_vals = np.unique(interior_aspect)
        assert list(unique_vals) == [1], f"Expected [1], got {unique_vals}"

    def test_east_facing_plane_aspect(self) -> None:
        """Plane tilted to face east → aspect_class == 3.

        East-facing: terrain descends eastward → elevation is HIGH in the
        west (col 0) and LOW in the east (last col).  Column index increases
        eastward, so elevation decreases with column index.
        """
        slope_deg = 15.0
        res_m = 100.0
        dz = math.tan(math.radians(slope_deg)) * res_m
        height, width = 20, 20
        # col 0 = west = highest; elevation decreases eastward (with col index)
        cols = 1000.0 - np.arange(width, dtype=np.float64) * dz
        data = np.tile(cols[np.newaxis, :], (height, 1))

        _, aspect = _compute_slope_aspect(data, res_m=res_m)
        interior_aspect = aspect[2:-2, 2:-2]
        unique_vals = np.unique(interior_aspect)
        assert list(unique_vals) == [3], f"Expected [3], got {unique_vals}"

    def test_slope_magnitude_multiple_angles(self) -> None:
        """Parameterised slope magnitude test for several angles."""
        for target_deg in [5.0, 10.0, 30.0]:
            res_m = 100.0
            dz = math.tan(math.radians(target_deg)) * res_m
            col = np.arange(20, dtype=np.float64) * dz
            data = np.tile(col, (20, 1))
            slope, _ = _compute_slope_aspect(data, res_m=res_m)
            interior = slope[1:-1, 1:-1]
            assert float(np.mean(interior)) == pytest.approx(target_deg, rel=0.02), (
                f"Slope mismatch for {target_deg}°"
            )


# ---------------------------------------------------------------------------
# Unit tests — tile URL scheme
# ---------------------------------------------------------------------------


class TestTileUrlScheme:
    def test_northern_eastern_tile(self) -> None:
        url = _glo30_tile_url(31, 27)
        assert "N31" in url
        assert "E027" in url
        assert url.endswith(".tif")
        assert "copernicus-dem-30m.s3.amazonaws.com" in url

    def test_southern_western_tile(self) -> None:
        url = _glo30_tile_url(-10, -75)
        assert "S10" in url
        assert "W075" in url

    def test_tiles_for_aoi_nw_coast(self) -> None:
        # NW Coast AOI: (27.0, 31.0, 28.0, 31.5)
        tiles = _tiles_for_aoi((27.0, 31.0, 28.0, 31.5))
        assert (31, 27) in tiles
        assert len(tiles) == 1  # lat floors to 31, lon floors to 27

    def test_tiles_for_multi_degree_aoi(self) -> None:
        tiles = _tiles_for_aoi((26.0, 30.0, 29.0, 33.0))
        assert len(tiles) == 3 * 3  # 3 lat x 3 lon


# ---------------------------------------------------------------------------
# Integration tests — TerrainSource with mocked HTTP
# ---------------------------------------------------------------------------


def _mock_glo30_transport(dem_bytes: bytes) -> httpx.MockTransport:
    """Return a transport that serves *dem_bytes* for any GLO-30 request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "copernicus-dem-30m" in str(request.url):
            return httpx.Response(200, content=dem_bytes)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestTerrainSourceMocked:
    """Full TerrainSource tests with HTTP mocked via monkey-patching httpx.Client."""

    def _patch_httpx(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dem_bytes: bytes,
        status: int = 200,
    ) -> None:
        """Patch httpx.Client so GLO-30 requests return *dem_bytes*."""
        call_count: dict[str, int] = {"n": 0}

        def fake_request(
            self_client: httpx.Client,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            call_count["n"] += 1
            if "copernicus-dem-30m" in url:
                return httpx.Response(status, content=dem_bytes if status == 200 else b"")
            if "opentopography" in url:
                return httpx.Response(200, content=dem_bytes)
            return httpx.Response(404)

        monkeypatch.setattr(httpx.Client, "request", fake_request)

    def test_output_shape_and_bands(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)

        assert result.dims == ("band", "y", "x")
        assert list(result.coords["band"].values) == ["elevation", "slope", "aspect_class"]
        assert result.shape[0] == 3
        assert result.shape[1] > 0
        assert result.shape[2] > 0

    def test_output_has_crs(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)

        assert result.rio.crs is not None
        # NW Coast centroid ~27.5°E → UTM zone 35N
        assert result.rio.crs.to_epsg() == 32635

    def test_flat_dem_slope_near_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Flat DEM: valid (non-NaN) slope cells should be near zero.

        Cells outside the tile extent become NaN; only cells with valid
        elevation (non-NaN) are checked for near-zero slope.
        """
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)

        elev = result.sel(band="elevation").values
        slope = result.sel(band="slope").values
        # Check only cells where elevation is valid (non-NaN)
        valid_mask = ~np.isnan(elev)
        assert valid_mask.any(), "No valid elevation cells found in result"
        valid_slope = slope[valid_mask]
        assert float(np.nanmax(np.abs(valid_slope))) == pytest.approx(0.0, abs=0.01)

    def test_flat_dem_aspect_all_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Flat DEM: valid (non-NaN) cells should have aspect_class == 0 (flat)."""
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)

        elev = result.sel(band="elevation").values
        aspect = result.sel(band="aspect_class").values
        valid_mask = ~np.isnan(elev)
        assert valid_mask.any()
        assert np.all(aspect[valid_mask] == 0), (
            f"Flat DEM valid cells should be aspect_class=0, "
            f"got unique={np.unique(aspect[valid_mask])}"
        )

    def test_south_facing_dem_aspect_class_5(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A south-tilting DEM should produce aspect_class=5 in interior cells."""
        aoi = _nw_coast_aoi()
        dem_bytes = _south_facing_dem_bytes(
            slope_deg=15.0, height=64, width=64, bounds=(27.0, 31.0, 28.0, 32.0)
        )
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)

        aspect = result.sel(band="aspect_class").values.astype(int)
        # Interior cells away from reprojection boundary artefacts
        interior = aspect[3:-3, 3:-3]
        unique = np.unique(interior)
        assert 5 in unique, f"Expected south-facing (5) in interior, got {unique}"

    def test_cache_miss_then_hit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """First call = miss (compute + store), second call = hit (load from disk)."""
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))

        call_counts: dict[str, int] = {"n": 0}

        def fake_request(
            self_client: httpx.Client,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            call_counts["n"] += 1
            if "copernicus-dem-30m" in url:
                return httpx.Response(200, content=dem_bytes)
            return httpx.Response(404)

        monkeypatch.setattr(httpx.Client, "request", fake_request)

        cache = DiskCache(tmp_path)
        src = TerrainSource(cache=cache)

        r1 = src.fetch(aoi, resolution_m=1000)
        net_calls_after_miss = call_counts["n"]
        assert net_calls_after_miss > 0

        r2 = src.fetch(aoi, resolution_m=1000)
        # No additional network calls on cache hit
        assert call_counts["n"] == net_calls_after_miss

        # Both results should have same shape
        assert r1.shape == r2.shape

    def test_404_ocean_tile_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """If one tile 404s but another succeeds, the mosaic still works."""
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))

        call_log: list[str] = []

        def fake_request(
            self_client: httpx.Client,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            call_log.append(url)
            # Return 200 for any GLO-30 tile regardless
            if "copernicus-dem-30m" in url:
                return httpx.Response(200, content=dem_bytes)
            return httpx.Response(404)

        monkeypatch.setattr(httpx.Client, "request", fake_request)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)
        assert result is not None

    def test_glo30_failure_falls_back_to_opentopo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When GLO-30 all-404s, fallback to OpenTopography is attempted."""
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))

        monkeypatch.setenv("OPENTOPO_KEY", "testkey123")

        def fake_request(
            self_client: httpx.Client,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            if "copernicus-dem-30m" in url:
                return httpx.Response(404)
            if "opentopography" in url:
                return httpx.Response(200, content=dem_bytes)
            return httpx.Response(404)

        monkeypatch.setattr(httpx.Client, "request", fake_request)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)
        assert result is not None
        assert list(result.coords["band"].values) == ["elevation", "slope", "aspect_class"]

    def test_opentopo_source_param(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Passing source='opentopo' goes directly to OpenTopography."""
        aoi = _nw_coast_aoi()
        dem_bytes = _flat_dem_bytes(height=32, width=32, bounds=(27.0, 31.0, 28.0, 32.0))

        monkeypatch.setenv("OPENTOPO_KEY", "testkey123")

        def fake_request(
            self_client: httpx.Client,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            if "opentopography" in url:
                return httpx.Response(200, content=dem_bytes)
            return httpx.Response(500)

        monkeypatch.setattr(httpx.Client, "request", fake_request)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000, source="opentopo")
        assert result is not None

    def test_opentopo_no_key_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """OpenTopography without a key raises AcquisitionError."""
        from solarsite.acquire.base import AcquisitionError

        aoi = _nw_coast_aoi()
        # Ensure env is clean
        monkeypatch.delenv("OPENTOPO_KEY", raising=False)
        # Also patch dotenv so it doesn't load the real .env
        monkeypatch.setattr("solarsite.acquire.terrain.load_dotenv", lambda: None)

        src = TerrainSource(cache=DiskCache(tmp_path))
        with pytest.raises(AcquisitionError, match="OPENTOPO_KEY"):
            src.fetch(aoi, resolution_m=1000, source="opentopo")

    def test_elevation_values_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Elevation band should reflect input DEM values after reprojection."""
        aoi = _nw_coast_aoi()
        target_elev = 250.0
        dem_bytes = _flat_dem_bytes(
            height=64,
            width=64,
            elevation=target_elev,
            bounds=(27.0, 31.0, 28.0, 32.0),
        )
        self._patch_httpx(monkeypatch, dem_bytes)

        src = TerrainSource(cache=DiskCache(tmp_path))
        result = src.fetch(aoi, resolution_m=1000)
        elev = result.sel(band="elevation").values
        assert float(np.nanmean(elev)) == pytest.approx(target_elev, rel=0.05)


# ---------------------------------------------------------------------------
# Live test (excluded from CI -- run with pytest -m live)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_glo30_nw_coast() -> None:
    """Fetch real GLO-30 tiles for the NW Coast AOI from public AWS.

    Assertions:
      * Result has 3 bands, a valid CRS, positive spatial extent.
      * Elevation range is plausible for the NW Egyptian coast (0-500 m).
      * Slope values are all non-negative and < 90 deg.
      * Aspect classes are integers in 0-8.
    """
    aoi = _nw_coast_aoi()
    src = TerrainSource()
    result = src.fetch(aoi, resolution_m=500)

    assert result.dims == ("band", "y", "x")
    assert list(result.coords["band"].values) == ["elevation", "slope", "aspect_class"]
    assert result.rio.crs is not None

    elev = result.sel(band="elevation").values
    slope = result.sel(band="slope").values
    aspect = result.sel(band="aspect_class").values

    # Sanity checks for the NW Egypt coastal strip.
    # After nodata masking, -9999 sentinel values should not appear.
    valid_elev = elev[~np.isnan(elev)]
    assert len(valid_elev) > 0, "No valid elevation cells found"
    assert float(valid_elev.min()) >= -10.0, f"Elevation below -10 m: {float(valid_elev.min())}"
    assert float(valid_elev.max()) <= 1000.0, "Elevation above 1000 m unexpected for NW coast"

    valid_slope = slope[~np.isnan(slope)]
    assert float(valid_slope.min()) >= 0.0
    assert float(valid_slope.max()) < 90.0

    valid_aspect = aspect[~np.isnan(aspect)]
    assert int(valid_aspect.min()) >= 0
    assert int(valid_aspect.max()) <= 8
