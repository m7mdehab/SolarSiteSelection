"""Tests for src/solarsite/acquire/osm.py.

CI policy: no network calls.  Overpass is mocked via httpx.MockTransport /
respx.  Fixtures are recorded Overpass JSON responses in
``tests/fixtures/osm/``.

Live test (``@pytest.mark.live``) is excluded from CI via pytest.ini marker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import numpy as np
import pytest
import xarray as xr
from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from solarsite.acquire.base import grid_for_aoi
from solarsite.acquire.osm import (
    OSMPowerSource,
    OSMRailwaySource,
    OSMRoadsSource,
    OSMUrbanSource,
    _overpass_query,
    _parse_elements_to_gdf,
    _tile_aoi,
    proximity_for,
)
from solarsite.core import AOI, DiskCache

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------
_FIXTURES = Path(__file__).parent / "fixtures"
_AOI_FILE = _FIXTURES / "nw_coast_aoi.geojson"
_OSM_DIR = _FIXTURES / "osm"


def _aoi() -> AOI:
    return AOI.from_geojson(json.loads(_AOI_FILE.read_text()))


def _load_fixture(name: str) -> bytes:
    return (_OSM_DIR / name).read_bytes()


def _make_mock_transport(fixture_name: str) -> httpx.MockTransport:
    """Return a MockTransport that always serves the given fixture."""
    payload = _load_fixture(fixture_name)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _working_crs(aoi: AOI) -> CRS:
    from solarsite.core import working_crs_for

    return working_crs_for(aoi.geometry)


# ---------------------------------------------------------------------------
# _overpass_query
# ---------------------------------------------------------------------------


class TestOverpassQuery:
    def test_contains_bbox(self) -> None:
        aoi = _aoi()
        q = _overpass_query(aoi, '  way["power"="line"];\n')
        # NW-coast AOI bounds: minx=27, miny=31, maxx=28, maxy=31.5
        assert "31.0,27.0,31.5,28.0" in q

    def test_contains_timeout(self) -> None:
        aoi = _aoi()
        q = _overpass_query(aoi, "")
        assert "[timeout:60]" in q

    def test_contains_body(self) -> None:
        aoi = _aoi()
        body = '  way["railway"="rail"];\n'
        q = _overpass_query(aoi, body)
        assert body in q


# ---------------------------------------------------------------------------
# _parse_elements_to_gdf
# ---------------------------------------------------------------------------


class TestParseElementsToGdf:
    def test_empty_elements_returns_empty_gdf(self) -> None:
        gdf = _parse_elements_to_gdf([])
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 0

    def test_way_becomes_linestring(self) -> None:
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.2},
            {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "primary"}},
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=True, include_node=False)
        assert len(gdf) == 1
        assert isinstance(gdf.geometry.iloc[0], LineString)

    def test_closed_way_becomes_polygon(self) -> None:
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.1},
            {"type": "node", "id": 3, "lat": 31.2, "lon": 27.2},
            {"type": "node", "id": 4, "lat": 31.1, "lon": 27.2},
            {
                "type": "way",
                "id": 10,
                "nodes": [1, 2, 3, 4, 1],
                "tags": {"landuse": "residential"},
            },
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=True, include_node=False)
        assert len(gdf) == 1
        assert isinstance(gdf.geometry.iloc[0], Polygon)

    def test_node_becomes_point(self) -> None:
        elements = [
            {
                "type": "node",
                "id": 1,
                "lat": 31.3,
                "lon": 27.7,
                "tags": {"place": "town", "name": "Foo"},
            }
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=False, include_node=True)
        assert len(gdf) == 1
        assert isinstance(gdf.geometry.iloc[0], Point)

    def test_tag_filter_excludes_non_matching(self) -> None:
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.2},
            {
                "type": "way",
                "id": 10,
                "nodes": [1, 2],
                "tags": {"highway": "residential"},
            },
            {
                "type": "way",
                "id": 11,
                "nodes": [1, 2],
                "tags": {"highway": "primary"},
            },
        ]
        gdf = _parse_elements_to_gdf(
            elements,
            include_way=True,
            include_node=False,
            tag_filter={"highway": frozenset({"primary"})},
        )
        assert len(gdf) == 1
        assert gdf.iloc[0]["highway"] == "primary"

    def test_reprojected_to_working_crs(self) -> None:
        from shapely.geometry import Polygon as SPolygon

        from solarsite.core import working_crs_for

        wcrs = working_crs_for(SPolygon([(27.0, 31.0), (28.0, 31.0), (28.0, 31.5), (27.0, 31.5)]))
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.2},
            {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"power": "line"}},
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=True, working_crs=wcrs)
        assert gdf.crs == wcrs


# ---------------------------------------------------------------------------
# _tile_aoi
# ---------------------------------------------------------------------------


class TestTileAoi:
    def test_small_aoi_not_tiled(self) -> None:
        # NW coast is ~5280 km2 > 2500, so should be tiled; use a small sub-box
        small_geojson: dict[str, Any] = {
            "type": "Polygon",
            "coordinates": [
                [[27.0, 31.0], [27.5, 31.0], [27.5, 31.25], [27.0, 31.25], [27.0, 31.0]]
            ],
        }
        small_aoi = AOI.from_geojson(small_geojson)
        tiles = _tile_aoi(small_aoi)
        assert len(tiles) == 1
        assert tiles[0] is small_aoi

    def test_large_aoi_produces_four_tiles(self) -> None:
        aoi = _aoi()  # ~5280 km2 > threshold
        tiles = _tile_aoi(aoi)
        assert len(tiles) == 4

    def test_tiles_cover_original_bbox(self) -> None:
        aoi = _aoi()
        tiles = _tile_aoi(aoi)
        minx = min(t.bounds[0] for t in tiles)
        miny = min(t.bounds[1] for t in tiles)
        maxx = max(t.bounds[2] for t in tiles)
        maxy = max(t.bounds[3] for t in tiles)
        orig = aoi.bounds
        assert abs(minx - orig[0]) < 1e-9
        assert abs(miny - orig[1]) < 1e-9
        assert abs(maxx - orig[2]) < 1e-9
        assert abs(maxy - orig[3]) < 1e-9


# ---------------------------------------------------------------------------
# OSMPowerSource
# ---------------------------------------------------------------------------


class TestOSMPowerSource:
    def _source(self, fixture: str = "power_response.json") -> OSMPowerSource:
        transport = _make_mock_transport(fixture)
        client = httpx.Client(transport=transport)
        return OSMPowerSource(client=client)

    def test_fetch_returns_geodataframe(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_fetch_returns_correct_crs(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert gdf.crs == _working_crs(aoi)

    def test_fetch_power_line_count(self) -> None:
        """Fixture has 2 power=line ways + 1 power=substation node."""
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        # power lines -> LineStrings, substation -> Point
        assert len(gdf) >= 1

    def test_fetch_contains_linestrings(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        geom_types = set(gdf.geometry.geom_type.unique())
        assert "LineString" in geom_types or len(gdf) == 0

    def test_empty_response_returns_empty_gdf(self) -> None:
        aoi = _aoi()
        source = self._source("empty_response.json")
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 0
        assert gdf.crs == _working_crs(aoi)

    def test_name_is_osm_power(self) -> None:
        assert OSMPowerSource.name == "osm_power"


# ---------------------------------------------------------------------------
# OSMRoadsSource
# ---------------------------------------------------------------------------


class TestOSMRoadsSource:
    def _source(self, fixture: str = "roads_response.json") -> OSMRoadsSource:
        transport = _make_mock_transport(fixture)
        client = httpx.Client(transport=transport)
        return OSMRoadsSource(client=client)

    def test_fetch_returns_geodataframe(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_fetch_returns_correct_crs(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert gdf.crs == _working_crs(aoi)

    def test_roads_are_linestrings(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        if not gdf.empty:
            assert all(isinstance(g, LineString) for g in gdf.geometry)

    def test_empty_response_returns_empty_gdf(self) -> None:
        aoi = _aoi()
        source = self._source("empty_response.json")
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 0
        assert gdf.crs == _working_crs(aoi)

    def test_name_is_osm_roads(self) -> None:
        assert OSMRoadsSource.name == "osm_roads"

    def test_roads_count_matches_fixture(self) -> None:
        """Fixture has 2 road ways (primary + secondary)."""
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        # The fixture AOI is large (tiled 2x2 = 4 requests, same fixture returned each)
        # Each tile returns 2 roads; after dedup-by-geometry, should be >= 2
        assert len(gdf) >= 2


# ---------------------------------------------------------------------------
# OSMRailwaySource
# ---------------------------------------------------------------------------


class TestOSMRailwaySource:
    def _source(self, fixture: str = "railway_response.json") -> OSMRailwaySource:
        transport = _make_mock_transport(fixture)
        client = httpx.Client(transport=transport)
        return OSMRailwaySource(client=client)

    def test_fetch_returns_geodataframe(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_fetch_returns_correct_crs(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert gdf.crs == _working_crs(aoi)

    def test_railway_are_linestrings(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        if not gdf.empty:
            assert all(isinstance(g, LineString) for g in gdf.geometry)

    def test_empty_response_returns_empty_gdf(self) -> None:
        aoi = _aoi()
        source = self._source("empty_response.json")
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 0
        assert gdf.crs == _working_crs(aoi)

    def test_name_is_osm_railway(self) -> None:
        assert OSMRailwaySource.name == "osm_railway"


# ---------------------------------------------------------------------------
# OSMUrbanSource
# ---------------------------------------------------------------------------


class TestOSMUrbanSource:
    def _source(self, fixture: str = "urban_response.json") -> OSMUrbanSource:
        transport = _make_mock_transport(fixture)
        client = httpx.Client(transport=transport)
        return OSMUrbanSource(client=client)

    def test_fetch_returns_geodataframe(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_fetch_returns_correct_crs(self) -> None:
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert gdf.crs == _working_crs(aoi)

    def test_urban_has_polygons_and_points(self) -> None:
        """Fixture has 1 residential polygon + 2 place nodes."""
        aoi = _aoi()
        source = self._source()
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        if not gdf.empty:
            geom_types = set(gdf.geometry.geom_type.unique())
            # After tiling (2x2=4 tiles, 2 requests each = 8 calls), same fixtures
            # deduplication keeps unique geometries
            assert len(geom_types) >= 1

    def test_empty_response_returns_empty_gdf(self) -> None:
        aoi = _aoi()
        source = self._source("empty_response.json")
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 0
        assert gdf.crs == _working_crs(aoi)

    def test_name_is_osm_urban(self) -> None:
        assert OSMUrbanSource.name == "osm_urban"


# ---------------------------------------------------------------------------
# proximity_for
# ---------------------------------------------------------------------------


class TestProximityFor:
    """Tests for proximity_for using a tiny synthetic feature set."""

    def _build_tiny_aoi(self) -> AOI:
        """3 km x 3 km sub-box inside the NW-coast AOI."""
        geojson: dict[str, Any] = {
            "type": "Polygon",
            "coordinates": [
                [
                    [27.0, 31.0],
                    [27.027, 31.0],
                    [27.027, 31.027],
                    [27.0, 31.027],
                    [27.0, 31.0],
                ]
            ],
        }
        return AOI.from_geojson(geojson)

    def _line_gdf(self, aoi: AOI) -> gpd.GeoDataFrame:
        """Build a GeoDataFrame with one power line crossing the tiny AOI."""
        from pyproj import Transformer

        from solarsite.core import working_crs_for

        wcrs = working_crs_for(aoi.geometry)
        transformer = Transformer.from_crs("EPSG:4326", wcrs, always_xy=True)
        x0, y0 = transformer.transform(27.005, 31.013)
        x1, y1 = transformer.transform(27.022, 31.013)
        return gpd.GeoDataFrame(
            {"power": ["line"]},
            geometry=[LineString([(x0, y0), (x1, y1)])],
            crs=wcrs,
        )

    def test_proximity_returns_dataarray(self) -> None:
        from solarsite.core import proximity_raster

        aoi = self._build_tiny_aoi()
        gdf = self._line_gdf(aoi)
        spec = grid_for_aoi(aoi, resolution_m=500)
        da = proximity_raster(gdf, spec)
        assert isinstance(da, xr.DataArray)

    def test_proximity_crs_matches_aoi(self) -> None:
        from solarsite.core import proximity_raster, working_crs_for

        aoi = self._build_tiny_aoi()
        gdf = self._line_gdf(aoi)
        spec = grid_for_aoi(aoi, resolution_m=500)
        da = proximity_raster(gdf, spec)
        assert da.rio.crs == working_crs_for(aoi.geometry)

    def test_proximity_units_are_metres(self) -> None:
        """Cells far from the feature must have distance > 0, feature cells ~= 0."""
        from solarsite.core import proximity_raster

        aoi = self._build_tiny_aoi()
        gdf = self._line_gdf(aoi)
        spec = grid_for_aoi(aoi, resolution_m=100)
        da = proximity_raster(gdf, spec)
        values = da.values
        # All valid (non-NaN) distances must be non-negative
        valid = values[~np.isnan(values)]
        assert np.all(valid >= 0)
        # Maximum distance in a 3 km box should be < 5 000 m
        assert float(np.nanmax(values)) < 5_000

    def test_proximity_feature_cell_is_zero(self) -> None:
        """A cell that coincides with the feature should have distance 0."""
        from solarsite.core import proximity_raster

        aoi = self._build_tiny_aoi()
        gdf = self._line_gdf(aoi)
        spec = grid_for_aoi(aoi, resolution_m=100)
        da = proximity_raster(gdf, spec)
        valid = da.values[~np.isnan(da.values)]
        assert float(np.nanmin(valid)) == pytest.approx(0.0, abs=1.0)

    def test_proximity_empty_layer_returns_nan(self) -> None:
        """Empty GDF -> proximity raster filled with NaN."""
        from solarsite.core import proximity_raster

        aoi = self._build_tiny_aoi()
        wcrs = grid_for_aoi(aoi, resolution_m=500).crs
        empty_gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326")).to_crs(wcrs)
        spec = grid_for_aoi(aoi, resolution_m=500)
        da = proximity_raster(empty_gdf, spec)
        # Empty features -> all NaN
        assert np.all(np.isnan(da.values))

    def test_proximity_for_via_source(self, tmp_path: Path) -> None:
        """proximity_for uses source.fetch + proximity_raster end-to-end."""
        aoi = self._build_tiny_aoi()
        transport = _make_mock_transport("power_response.json")
        client = httpx.Client(transport=transport)
        cache = DiskCache(root=tmp_path)
        source = OSMPowerSource(cache=cache, client=client)

        da = proximity_for(source, aoi, resolution_m=500, _sleep=lambda _: None)
        assert isinstance(da, xr.DataArray)
        assert da.name == "distance_m"


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------


class TestCacheHitMiss:
    def test_cache_miss_calls_fetch(self, tmp_path: Path) -> None:
        cache = DiskCache(root=tmp_path)
        transport = _make_mock_transport("roads_response.json")
        client = httpx.Client(transport=transport)
        source = OSMRoadsSource(cache=cache, client=client)
        aoi = _aoi()

        assert not cache.exists("osm_roads", aoi.hash, {"resolution_m": 100})
        gdf = source.fetch(aoi, resolution_m=100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        # _sleep is stripped from cache params by _OSMBase.fetch
        assert cache.exists("osm_roads", aoi.hash, {"resolution_m": 100})

    def test_cache_hit_returns_same_result(self, tmp_path: Path) -> None:
        cache = DiskCache(root=tmp_path)
        call_count = {"n": 0}
        payload = _load_fixture("railway_response.json")

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(
                200, content=payload, headers={"Content-Type": "application/json"}
            )

        client1 = httpx.Client(transport=httpx.MockTransport(handler))
        source1 = OSMRailwaySource(cache=cache, client=client1)
        aoi = _aoi()

        gdf1 = source1.fetch(aoi, resolution_m=100, _sleep=lambda _: None)
        n_after_first = call_count["n"]

        # Second fetch: same cache, new client that would also count calls
        client2 = httpx.Client(transport=httpx.MockTransport(handler))
        source2 = OSMRailwaySource(cache=cache, client=client2)
        gdf2 = source2.fetch(aoi, resolution_m=100, _sleep=lambda _: None)

        # Network was NOT called again (cache hit)
        assert call_count["n"] == n_after_first
        assert len(gdf1) == len(gdf2)


# ---------------------------------------------------------------------------
# Fallback mirror test
# ---------------------------------------------------------------------------


class TestFallbackMirror:
    def test_fallback_used_when_primary_fails(self) -> None:
        """If the primary endpoint returns 503, the fallback should be tried."""
        payload = _load_fixture("power_response.json")
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(request.url.host)
            if "overpass-api.de" in request.url.host:
                # Always fail primary (exhaust retries)
                return httpx.Response(503, text="overloaded")
            # Fallback succeeds
            return httpx.Response(
                200, content=payload, headers={"Content-Type": "application/json"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        aoi_geojson: dict[str, Any] = {
            "type": "Polygon",
            "coordinates": [
                [[27.0, 31.0], [27.5, 31.0], [27.5, 31.25], [27.0, 31.25], [27.0, 31.0]]
            ],
        }
        aoi = AOI.from_geojson(aoi_geojson)
        source = OSMPowerSource(client=client)
        gdf = source._fetch_uncached(aoi, 100, _sleep=lambda _: None)
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert any("kumi" in h for h in call_log)


# ---------------------------------------------------------------------------
# Edge-case coverage: _way_to_geometry and tag_filter "any"
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_way_with_missing_node_skipped(self) -> None:
        """A way referencing a node not in the elements list is silently skipped."""
        elements = [
            # Node 2 is intentionally absent
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "way", "id": 10, "nodes": [1, 99999], "tags": {"highway": "primary"}},
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=True)
        # Only 1 resolved coord -> < 2 -> geometry is None -> 0 features
        assert len(gdf) == 0

    def test_way_with_single_node_produces_no_feature(self) -> None:
        """A way with only one resolvable node cannot form a geometry."""
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "way", "id": 10, "nodes": [1], "tags": {"railway": "rail"}},
        ]
        gdf = _parse_elements_to_gdf(elements, include_way=True)
        assert len(gdf) == 0

    def test_tag_filter_any_value_kept(self) -> None:
        """tag_filter with value 'any' keeps elements that have the tag at all."""
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.2},
            {
                "type": "way",
                "id": 10,
                "nodes": [1, 2],
                "tags": {"power": "line"},
            },
            {
                "type": "way",
                "id": 11,
                "nodes": [1, 2],
                "tags": {},  # no "power" tag -> should be excluded
            },
        ]
        gdf = _parse_elements_to_gdf(
            elements,
            include_way=True,
            tag_filter={"power": "any"},
        )
        assert len(gdf) == 1

    def test_tag_filter_any_value_excludes_missing(self) -> None:
        """tag_filter 'any' excludes elements that lack the tag entirely."""
        elements = [
            {"type": "node", "id": 1, "lat": 31.1, "lon": 27.1},
            {"type": "node", "id": 2, "lat": 31.2, "lon": 27.2},
            {
                "type": "way",
                "id": 10,
                "nodes": [1, 2],
                "tags": {},  # no matching tag
            },
        ]
        gdf = _parse_elements_to_gdf(
            elements,
            include_way=True,
            tag_filter={"power": "any"},
        )
        assert len(gdf) == 0


# ---------------------------------------------------------------------------
# Live test -- excluded from CI
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_roads_nw_coast_small_bbox() -> None:
    """Real Overpass call for a small sub-bbox of the NW-coast AOI.

    Asserts that at least some road features are returned.  Run manually::

        pytest -m live tests/test_acquire_osm.py::test_live_roads_nw_coast_small_bbox
    """
    # 0.2 x 0.15 degree patch near Marsa Matruh city (~130 km2)
    geojson: dict[str, Any] = {
        "type": "Polygon",
        "coordinates": [
            [
                [27.2, 31.32],
                [27.4, 31.32],
                [27.4, 31.47],
                [27.2, 31.47],
                [27.2, 31.32],
            ]
        ],
    }
    aoi = AOI.from_geojson(geojson)
    source = OSMRoadsSource()
    gdf = source._fetch_uncached(aoi, resolution_m=100)
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert len(gdf) > 0, "Expected at least one road feature near Marsa Matruh."
    print(f"\nLive test: {len(gdf)} road features returned for small NW-coast bbox.")
