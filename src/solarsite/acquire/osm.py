"""OSM Overpass acquisition: power lines, roads, railways, urban areas.

Each public :class:`VectorSource` subclass queries the Overpass API, parses
the JSON response into a :class:`geopandas.GeoDataFrame` in the working UTM CRS,
and returns it.  :func:`proximity_for` wraps a source with the P0.2 proximity
kernel to produce a distance-in-metres :class:`xarray.DataArray`.

Overpass etiquette
------------------
* Requests carry a descriptive ``User-Agent`` header.
* :func:`~solarsite.acquire.base.request_with_retry` handles 429 / 504 with
  exponential backoff (up to 4 retries, starting at 1.5 s).
* Large AOIs (area > ``TILE_AREA_THRESHOLD_KM2`` = 2 500 km2) are split into a
  2 x 2 tile grid and queried per-tile; results are concatenated.  This keeps
  each Overpass bounding-box manageable and avoids gateway timeouts.

Proximity raster units
-----------------------
:func:`proximity_for` returns an :class:`xarray.DataArray` named
``"distance_m"`` in **metres**.  The criteria breakpoints in
``configs/criteria.yaml`` are expressed in **kilometres**; callers must
divide by 1000 before applying breakpoints.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import geopandas as gpd
import httpx
import rioxarray  # noqa: F401 -- registers the .rio accessor on xr.DataArray
import xarray as xr
from shapely.geometry import LineString, Point, Polygon

from solarsite.acquire.base import VectorSource, grid_for_aoi, request_with_retry
from solarsite.core import AOI, GridSpec, proximity_raster, working_crs_for

__all__ = [
    "OSMPowerSource",
    "OSMRailwaySource",
    "OSMRoadsSource",
    "OSMUrbanSource",
    "proximity_for",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Overpass endpoints
# ---------------------------------------------------------------------------
_PRIMARY_ENDPOINT = "https://overpass-api.de/api/interpreter"
_FALLBACK_ENDPOINT = "https://overpass.kumi.systems/api/interpreter"

_USER_AGENT = (
    "SolarSiteSelection/0.1 (solar PV siting research; "
    "github.com/solarsite; contact: osm@solarsite.example)"
)

# ---------------------------------------------------------------------------
# Tiling threshold
# AOIs larger than this are split into a 2x2 tile grid before querying.
# 2 500 km2 ~= 50 km x 50 km per tile -- safely below Overpass timeout limits.
# ---------------------------------------------------------------------------
TILE_AREA_THRESHOLD_KM2: float = 2_500.0

# ---------------------------------------------------------------------------
# Highway tags captured for osm_roads
# Includes both base tags and their *_link variants (slip roads / ramps).
# ---------------------------------------------------------------------------
_ROAD_HIGHWAY_VALUES: frozenset[str] = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
    }
)

# ---------------------------------------------------------------------------
# Place tags captured for osm_urban (nodes with place=*)
# ---------------------------------------------------------------------------
_URBAN_PLACE_VALUES: frozenset[str] = frozenset({"city", "town", "village"})


# ---------------------------------------------------------------------------
# Internal Overpass helpers
# ---------------------------------------------------------------------------


def _overpass_query(aoi: AOI, ql_body: str) -> str:
    """Wrap a QL body with an AOI bounding-box and output settings.

    The ``[bbox:…]`` global setting restricts all statements to the AOI
    bounding box automatically.  We request JSON output with full metadata.

    Args:
        aoi: AOI whose WGS-84 bounding box constrains the query.
        ql_body: Overpass QL statements (without surrounding ``[out:json]``
                 wrapper or final ``out body;`` directive).

    Returns:
        Complete Overpass QL query string.
    """
    minx, miny, maxx, maxy = aoi.bounds  # (minx, miny, maxx, maxy) in WGS-84
    # Overpass bbox order: south, west, north, east
    bbox = f"{miny},{minx},{maxy},{maxx}"
    return f"[out:json][timeout:60][bbox:{bbox}];\n(\n{ql_body}\n);\nout body;\n>;\nout skel qt;"


def _fetch_overpass(
    query: str,
    *,
    client: httpx.Client | None = None,
    sleep: Any = None,
) -> dict[str, Any]:
    """POST a query to Overpass, falling back to mirror on failure.

    Args:
        query: Complete Overpass QL query string.
        client: Optional pre-built ``httpx.Client`` (e.g. for testing with
                ``MockTransport``).  If *None*, a new client is created.
        sleep: Optional sleep callable injected for testing (bypasses real waits).

    Returns:
        Parsed JSON response dict from Overpass.

    Raises:
        AcquisitionError: If both primary and fallback endpoints fail.
    """
    import time as _time

    sleep_fn = sleep if sleep is not None else _time.sleep

    headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"}

    def _do_fetch(c: httpx.Client, url: str) -> dict[str, Any]:
        resp = request_with_retry(
            c,
            "POST",
            url,
            data={"data": query},
            headers=headers,
            retries=4,
            backoff=1.5,
            sleep=sleep_fn,
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    if client is not None:
        # Injected client (tests): try primary, fall back to mirror
        try:
            return _do_fetch(client, _PRIMARY_ENDPOINT)
        except Exception:
            return _do_fetch(client, _FALLBACK_ENDPOINT)

    # Production: create a fresh client with reasonable timeouts
    with httpx.Client(timeout=httpx.Timeout(90.0)) as prod_client:
        try:
            return _do_fetch(prod_client, _PRIMARY_ENDPOINT)
        except Exception:
            log.warning("Primary Overpass endpoint failed, trying fallback mirror.")
    with httpx.Client(timeout=httpx.Timeout(90.0)) as prod_client:
        return _do_fetch(prod_client, _FALLBACK_ENDPOINT)


def _tile_aoi(aoi: AOI) -> list[AOI]:
    """Split an AOI into a 2x2 grid of sub-AOIs for large areas.

    When the AOI area exceeds ``TILE_AREA_THRESHOLD_KM2``, we split it into
    four equal tiles so each Overpass query covers a smaller bounding box.

    Args:
        aoi: The original AOI.

    Returns:
        List of sub-AOIs (1 item if area ≤ threshold, 4 items otherwise).
    """
    if aoi.area_km2 <= TILE_AREA_THRESHOLD_KM2:
        return [aoi]

    minx, miny, maxx, maxy = aoi.bounds
    midx = (minx + maxx) / 2.0
    midy = (miny + maxy) / 2.0

    tiles: list[AOI] = []
    for x0, x1 in [(minx, midx), (midx, maxx)]:
        for y0, y1 in [(miny, midy), (midy, maxy)]:
            tile_geojson: dict[str, Any] = {
                "type": "Polygon",
                "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
            }
            with contextlib.suppress(Exception):
                tiles.append(AOI.from_geojson(tile_geojson))
    return tiles if tiles else [aoi]


def _nodes_index(elements: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Build a node-id → element dict for fast way-node lookup."""
    return {el["id"]: el for el in elements if el.get("type") == "node"}


def _way_to_geometry(
    way: dict[str, Any], nodes: dict[int, dict[str, Any]]
) -> LineString | Polygon | None:
    """Convert an Overpass way element to a Shapely geometry.

    A closed way (first node == last node, ≥4 refs) is treated as a Polygon;
    open ways become LineStrings.

    Args:
        way: Overpass element dict with ``type == "way"``.
        nodes: Mapping from node-id to Overpass node element.

    Returns:
        Shapely geometry or *None* if insufficient nodes are resolved.
    """
    node_ids: list[int] = way.get("nodes", [])
    coords: list[tuple[float, float]] = []
    for nid in node_ids:
        node = nodes.get(nid)
        if node is None:
            continue
        coords.append((float(node["lon"]), float(node["lat"])))

    if len(coords) < 2:
        return None

    # Closed ring with ≥ 4 refs (first == last) → polygon
    if len(coords) >= 4 and coords[0] == coords[-1]:
        try:
            return Polygon(coords)
        except Exception:
            return LineString(coords[:-1])  # degenerate fallback

    return LineString(coords)


def _parse_elements_to_gdf(
    elements: list[dict[str, Any]],
    *,
    include_way: bool = True,
    include_node: bool = False,
    tag_filter: dict[str, frozenset[str] | str] | None = None,
    working_crs: Any = None,
) -> gpd.GeoDataFrame:
    """Convert raw Overpass elements to a GeoDataFrame in the working CRS.

    Args:
        elements: ``elements`` list from the Overpass JSON response.
        include_way: Whether to include way geometries.
        include_node: Whether to include node geometries (point features).
        tag_filter: Optional ``{tag_key: frozenset_of_values}`` or
                    ``{tag_key: "any"}`` filter applied to element tags.
                    Only elements matching ALL specified conditions are kept.
        working_crs: Target CRS (pyproj CRS).  If *None*, the result is in
                     WGS-84.

    Returns:
        GeoDataFrame in *working_crs* (or WGS-84 if *None*).
    """
    nodes = _nodes_index(elements)
    geometries: list[Any] = []
    tags_list: list[dict[str, str]] = []

    for el in elements:
        etype = el.get("type")
        el_tags: dict[str, str] = el.get("tags", {})

        # Apply tag filter
        if tag_filter:
            match = True
            for key, allowed in tag_filter.items():
                val = el_tags.get(key)
                if allowed == "any":
                    if val is None:
                        match = False
                        break
                else:
                    if val not in allowed:  # type: ignore[operator]
                        match = False
                        break
            if not match:
                continue

        if etype == "way" and include_way:
            geom = _way_to_geometry(el, nodes)
            if geom is not None:
                geometries.append(geom)
                tags_list.append(el_tags)
        elif etype == "node" and include_node:
            lat = el.get("lat")
            lon = el.get("lon")
            if lat is not None and lon is not None:
                geometries.append(Point(float(lon), float(lat)))
                tags_list.append(el_tags)

    if not geometries:
        gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326"))
        if working_crs is not None:
            gdf = gdf.to_crs(working_crs)
        return gdf

    gdf = gpd.GeoDataFrame(tags_list, geometry=geometries, crs="EPSG:4326")
    if working_crs is not None:
        gdf = gdf.to_crs(working_crs)
    return gdf


def _fetch_and_concat(
    ql_body_fn: Any,
    aoi: AOI,
    *,
    client: httpx.Client | None = None,
    sleep: Any = None,
    include_way: bool = True,
    include_node: bool = False,
    tag_filter: dict[str, frozenset[str] | str] | None = None,
) -> gpd.GeoDataFrame:
    """Tile the AOI if necessary, fetch each tile, concatenate results.

    Args:
        ql_body_fn: Callable that takes an ``AOI`` and returns the Overpass QL
                    body string (without the outer ``[out:json]`` wrapper).
        aoi: The AOI to query.
        client: Optional injected ``httpx.Client`` for testing.
        sleep: Optional sleep callable for testing.
        include_way: Forward to :func:`_parse_elements_to_gdf`.
        include_node: Forward to :func:`_parse_elements_to_gdf`.
        tag_filter: Forward to :func:`_parse_elements_to_gdf`.

    Returns:
        Concatenated GeoDataFrame in the AOI working CRS (empty if no features).
    """
    wcrs = working_crs_for(aoi.geometry)
    tiles = _tile_aoi(aoi)

    frames: list[gpd.GeoDataFrame] = []
    for tile in tiles:
        query = _overpass_query(tile, ql_body_fn(tile))
        data = _fetch_overpass(query, client=client, sleep=sleep)
        elements: list[dict[str, Any]] = data.get("elements", [])
        gdf = _parse_elements_to_gdf(
            elements,
            include_way=include_way,
            include_node=include_node,
            tag_filter=tag_filter,
            working_crs=wcrs,
        )
        frames.append(gdf)

    if not frames or all(f.empty for f in frames):
        empty = gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326")).to_crs(wcrs)
        return empty

    result = gpd.GeoDataFrame(
        gpd.pd.concat([f for f in frames if not f.empty], ignore_index=True),
        crs=wcrs,
    )
    # Drop exact geometry duplicates that arise when tile boundaries
    # intersect features shared between neighbouring tiles.
    return gpd.GeoDataFrame(result.drop_duplicates(subset=["geometry"]), crs=wcrs)


# ---------------------------------------------------------------------------
# VectorSource subclasses
# ---------------------------------------------------------------------------


class _OSMBase(VectorSource):
    """Shared base for all OSM sources.

    Overrides :meth:`fetch` to strip the ``_sleep`` test-injection keyword
    from the cache-key parameters before delegating to :class:`DiskCache`.
    The ``_sleep`` callable is non-serialisable so it must not appear in the
    JSON-based cache key; it is only meaningful during ``_fetch_uncached``.
    """

    def fetch(self, aoi: AOI, resolution_m: int = 100, **params: Any) -> gpd.GeoDataFrame:
        # Pop _sleep so DiskCache never tries to json-serialise it.
        sleep = params.pop("_sleep", None)
        cache_params: dict[str, Any] = {"resolution_m": resolution_m, **params}
        return self._cache.get_or_compute(
            source=self.name,
            aoi_hash=aoi.hash,
            params=cache_params,
            compute_fn=lambda: self._fetch_uncached(aoi, resolution_m, _sleep=sleep, **params),
        )


class OSMPowerSource(_OSMBase):
    """Fetch power lines and substations from Overpass.

    Queries ``power=line`` (transmission/distribution lines as ways) and
    ``power=substation`` (substation nodes).

    The returned GeoDataFrame contains:
    * LineString geometries for ``power=line`` ways.
    * Point geometries for ``power=substation`` nodes.

    All geometries are in the AOI working CRS (UTM metres).
    """

    name: str = "osm_power"

    def __init__(self, cache: Any = None, *, client: httpx.Client | None = None) -> None:
        super().__init__(cache)
        self._client = client

    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> gpd.GeoDataFrame:
        sleep = params.get("_sleep")

        def ql_body(_tile: AOI) -> str:
            return '  way["power"="line"];\n  node["power"="substation"];\n'

        return _fetch_and_concat(
            ql_body,
            aoi,
            client=self._client,
            sleep=sleep,
            include_way=True,
            include_node=True,
            tag_filter=None,  # filter applied via QL
        )


class OSMRoadsSource(_OSMBase):
    """Fetch major roads from Overpass.

    Queries ``highway`` in {motorway, trunk, primary, secondary} plus their
    ``_link`` variants (slip roads, ramps).  Returns LineString geometries in
    the AOI working CRS.
    """

    name: str = "osm_roads"

    def __init__(self, cache: Any = None, *, client: httpx.Client | None = None) -> None:
        super().__init__(cache)
        self._client = client

    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> gpd.GeoDataFrame:
        sleep = params.get("_sleep")

        def ql_body(_tile: AOI) -> str:
            # Build one way selector per highway type to stay QL-compatible
            lines = []
            for htype in sorted(_ROAD_HIGHWAY_VALUES):
                lines.append(f'  way["highway"="{htype}"];\n')
            return "".join(lines)

        gdf = _fetch_and_concat(
            ql_body,
            aoi,
            client=self._client,
            sleep=sleep,
            include_way=True,
            include_node=False,
            tag_filter={"highway": _ROAD_HIGHWAY_VALUES},
        )
        return gdf


class OSMRailwaySource(_OSMBase):
    """Fetch mainline railways from Overpass.

    Queries ``railway=rail`` ways only (excludes light rail, tram, subway).
    Returns LineString geometries in the AOI working CRS.
    """

    name: str = "osm_railway"

    def __init__(self, cache: Any = None, *, client: httpx.Client | None = None) -> None:
        super().__init__(cache)
        self._client = client

    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> gpd.GeoDataFrame:
        sleep = params.get("_sleep")

        def ql_body(_tile: AOI) -> str:
            return '  way["railway"="rail"];\n'

        return _fetch_and_concat(
            ql_body,
            aoi,
            client=self._client,
            sleep=sleep,
            include_way=True,
            include_node=False,
            tag_filter={"railway": frozenset({"rail"})},
        )


class OSMUrbanSource(_OSMBase):
    """Fetch urban area features from Overpass.

    Queries:
    * ``landuse=residential`` polygon ways.
    * ``place`` in {city, town, village} nodes (administrative centres).

    Returns a mix of Polygon and Point geometries in the AOI working CRS.
    The combined layer represents human settlement proximity; the
    ``dist_urban`` criterion measures distance to the nearest feature in
    this GeoDataFrame.
    """

    name: str = "osm_urban"

    def __init__(self, cache: Any = None, *, client: httpx.Client | None = None) -> None:
        super().__init__(cache)
        self._client = client

    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> gpd.GeoDataFrame:
        sleep = params.get("_sleep")
        wcrs = working_crs_for(aoi.geometry)
        tiles = _tile_aoi(aoi)

        _ql_residential_body = '  way["landuse"="residential"];\n'
        _ql_places_body = "".join(
            f'  node["place"="{ptype}"];\n' for ptype in sorted(_URBAN_PLACE_VALUES)
        )

        frames: list[gpd.GeoDataFrame] = []
        for tile in tiles:
            # --- residential polygons ---
            query_res = _overpass_query(tile, _ql_residential_body)
            data_res = _fetch_overpass(query_res, client=self._client, sleep=sleep)
            gdf_res = _parse_elements_to_gdf(
                data_res.get("elements", []),
                include_way=True,
                include_node=False,
                tag_filter={"landuse": frozenset({"residential"})},
                working_crs=wcrs,
            )
            frames.append(gdf_res)

            # --- place nodes ---
            query_place = _overpass_query(tile, _ql_places_body)
            data_place = _fetch_overpass(query_place, client=self._client, sleep=sleep)
            gdf_place = _parse_elements_to_gdf(
                data_place.get("elements", []),
                include_way=False,
                include_node=True,
                tag_filter={"place": _URBAN_PLACE_VALUES},
                working_crs=wcrs,
            )
            frames.append(gdf_place)

        non_empty = [f for f in frames if not f.empty]
        if not non_empty:
            return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs="EPSG:4326")).to_crs(wcrs)

        result = gpd.GeoDataFrame(
            gpd.pd.concat(non_empty, ignore_index=True),
            crs=wcrs,
        )
        return gpd.GeoDataFrame(result.drop_duplicates(subset=["geometry"]), crs=wcrs)


# ---------------------------------------------------------------------------
# Proximity helper
# ---------------------------------------------------------------------------


def proximity_for(
    source: VectorSource,
    aoi: AOI,
    resolution_m: int = 100,
    **params: Any,
) -> xr.DataArray:
    """Fetch a vector layer and compute a Euclidean distance raster.

    This is a convenience wrapper around :meth:`VectorSource.fetch` and
    :func:`~solarsite.core.proximity_raster`.

    Units
    -----
    The returned DataArray (named ``"distance_m"``) stores distances in
    **metres**.  The ``dist_*`` criteria breakpoints in
    ``configs/criteria.yaml`` are in **km**; divide by 1 000 before scoring.

    Args:
        source: Any :class:`VectorSource` (OSMPowerSource, etc.).
        aoi: The AOI to query and rasterise over.
        resolution_m: Raster cell size in metres.
        **params: Additional keyword arguments forwarded to ``source.fetch``.

    Returns:
        :class:`xarray.DataArray` of Euclidean distances in metres, aligned
        to the AOI grid at ``resolution_m`` cell size.
    """
    gdf: gpd.GeoDataFrame = source.fetch(aoi, resolution_m=resolution_m, **params)
    spec: GridSpec = grid_for_aoi(aoi, resolution_m)
    return proximity_raster(gdf, spec)
