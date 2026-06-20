"""Site extraction: connected-component detection, polygonization, and ranking (P2.3).

Overview
--------
``extract_sites`` converts a classified LSI raster into a ranked
:class:`geopandas.GeoDataFrame` of candidate PV sites.  The pipeline is:

1. **Connected components** - identify contiguous regions of "top-class" cells
   using :func:`scipy.ndimage.label` with 8-connectivity (queen's case).
   8-connectivity is used so that diagonally adjacent cells form a single
   region, which produces more geographically coherent site polygons than
   4-connectivity.

2. **Minimum-area filter** - discard regions whose area
   (cell_count * cell_area_m2) falls below ``min_area_km2`` (default 0.5 km2).

3. **Polygonization** - convert each surviving region's binary raster mask to
   one or more exterior polygons via :func:`rasterio.features.shapes`.  When
   multiple shapes emerge from a single labeled region (holes / artefacts),
   they are unioned into a single MultiPolygon via
   :func:`shapely.ops.unary_union`.  Shapes may optionally be simplified with
   the Douglas-Peucker algorithm (``simplify_tolerance_m``).

4. **Per-site statistics** (all NaN-safe):

   ==================  ===========================================================
   Column              Description
   ==================  ===========================================================
   ``site_id``         Integer identifier (0-based internal label number).
   ``area_km2``        Site area in km2, derived from labeled-pixel count * cell
                       area.
   ``mean_lsi``        Mean of the *continuous* LSI DataArray over the site's
                       cells.
   ``max_lsi``         Maximum continuous LSI over the site's cells.
   ``centroid_x``      Centroid x-coordinate in the working CRS (metres).
   ``centroid_y``      Centroid y-coordinate in the working CRS (metres).
   ``centroid_lon``    Centroid longitude in WGS-84 (degrees E).
   ``centroid_lat``    Centroid latitude in WGS-84 (degrees N).
   ``mean_ptl_dist``   Mean distance to nearest PTL (metres), if
                       ``distance_to_ptl`` is supplied; else NaN.
   ``rank``            Rank (1 = best) after composite ordering (see below).
   ==================  ===========================================================

5. **Ranking composite** - sites are sorted by the following keys in order of
   precedence (all tiebreaks applied left-to-right):

   1. ``mean_lsi`` descending - higher suitability scores first.
   2. ``area_km2`` descending - larger sites rank higher among ties.
   3. ``mean_ptl_dist`` ascending - closer to power-transmission lines is
      better (lower impedance, lower connection cost).  If
      ``distance_to_ptl`` was not supplied, this column is NaN and ties on
      the first two keys are broken by the internal ``site_id`` (ascending),
      which ensures a deterministic ordering.

6. **Return** - the top-``top_k`` rows (or all rows if fewer survive the
   filter) as a :class:`geopandas.GeoDataFrame` with ``geometry`` in the
   same working CRS as the input rasters.  An empty DataFrame (no passing
   sites) is returned without raising.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from typing import cast

import geopandas as gpd
import numpy as np
import xarray as xr
from pyproj import Transformer
from rasterio.features import shapes as rasterio_shapes
from rasterio.transform import Affine
from scipy.ndimage import label as ndimage_label
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union

__all__ = ["SITE_COLUMNS", "extract_sites"]

# Canonical column order (geometry column is added by GeoDataFrame separately)
SITE_COLUMNS: list[str] = [
    "site_id",
    "area_km2",
    "mean_lsi",
    "max_lsi",
    "centroid_x",
    "centroid_y",
    "centroid_lon",
    "centroid_lat",
    "mean_ptl_dist",
    "rank",
]

# 8-connectivity (queen's case) structuring element for scipy.ndimage.label
_CONNECTIVITY_8 = np.ones((3, 3), dtype=np.int32)


def _get_transform(da: xr.DataArray) -> Affine:
    """Extract an affine transform from a DataArray.

    Tries rioxarray first; falls back to reconstructing from coordinate arrays.
    """
    try:
        tx = da.rio.transform()
        if tx is not None:
            return tx  # type: ignore[return-value]
    except Exception:  # broad catch: rioxarray may raise many error types
        pass

    # Reconstruct from 1-D coordinate arrays (cell-centre convention)
    x_vals = da.coords["x"].values.astype(float)
    y_vals = da.coords["y"].values.astype(float)

    if x_vals.size < 2 or y_vals.size < 2:
        # Single-cell edge case: assume 1 m resolution
        dx = 1.0
        dy = -1.0
    else:
        dx = float(x_vals[1] - x_vals[0])
        dy = float(y_vals[1] - y_vals[0])  # negative (north-up)

    # Cell-centre to top-left corner: shift by half a pixel
    west = float(x_vals[0]) - dx / 2.0
    north = float(y_vals[0]) - dy / 2.0

    return Affine(dx, 0.0, west, 0.0, dy, north)


def _cell_area_km2(transform: Affine) -> float:
    """Return the area of one cell in km2, derived from the affine transform.

    For a north-up grid with no rotation: |a| * |e| where *a* is the
    x-pixel size and *e* is the y-pixel size (both in CRS metres).
    """
    px_m = abs(transform.a)
    py_m = abs(transform.e)
    return (px_m * py_m) / 1_000_000.0


def _crs_to_epsg_string(da: xr.DataArray) -> str | None:
    """Return the working CRS as 'EPSG:XXXX' string, or None."""
    try:
        crs = da.rio.crs
        if crs is not None:
            epsg = crs.to_epsg()
            if epsg is not None:
                return f"EPSG:{epsg}"
    except Exception:  # broad catch: rioxarray may raise many error types
        pass
    return None


def _split_large_regions(
    labeled_arr: np.ndarray,
    num_features: int,
    cell_area_km2: float,
    max_area_km2: float | None,
) -> tuple[np.ndarray, int]:
    """Subdivide any connected component larger than ``max_area_km2`` into a grid
    of realistically-sized sub-sites (the A2 presentation fix).

    8-connectivity fuses an entire high-LSI region into one polygon, so the
    pipeline could present an 834 km² region as a single "site" (the misleading
    61,557-GWh figure). Here, an oversized component is partitioned along a
    deterministic square grid (tile side ≈ √max_cells) so each tile becomes its
    own candidate site of at most ``max_area_km2``. Total area is preserved; the
    min-area filter still drops slivers downstream. ``None`` disables splitting
    (backward-compatible default).
    """
    if max_area_km2 is None or cell_area_km2 <= 0:
        return labeled_arr, num_features
    max_cells = max(1, int(max_area_km2 / cell_area_km2))
    side = max(1, round(math.sqrt(max_cells)))

    out = np.zeros_like(labeled_arr)
    next_label = 0
    for lab in range(1, num_features + 1):
        rows, cols = np.where(labeled_arr == lab)
        if rows.size == 0:
            continue
        if rows.size <= max_cells:
            next_label += 1
            out[rows, cols] = next_label
            continue
        # Grid-partition the region's cells into ~max_cells tiles.
        rmin, cmin = rows.min(), cols.min()
        tiles_per_row = (cols.max() - cmin) // side + 1
        tile_r = (rows - rmin) // side
        tile_c = (cols - cmin) // side
        tile_id = tile_r * tiles_per_row + tile_c
        for tid in np.unique(tile_id):
            sel = tile_id == tid
            next_label += 1
            out[rows[sel], cols[sel]] = next_label
    return out, next_label


def extract_sites(
    lsi: xr.DataArray,
    class_raster: xr.DataArray,
    *,
    top_classes: Sequence[int] = (5,),
    min_area_km2: float = 0.5,
    max_site_area_km2: float | None = None,
    distance_to_ptl: xr.DataArray | None = None,
    top_k: int = 10,
    simplify_tolerance_m: float = 0.0,
) -> gpd.GeoDataFrame:
    """Extract, filter, and rank candidate PV sites from an LSI class raster.

    Parameters
    ----------
    lsi:
        Continuous LSI DataArray (values typically in [0, 1]).  Shape must
        match ``class_raster``.  Used for per-site mean and max statistics.
    class_raster:
        Integer class raster produced by
        :func:`~solarsite.analysis.overlay.classify_lsi`
        (values in [1, 5]; ``-9999`` = NODATA).  Cells whose class is in
        ``top_classes`` seed the connected-component search.
    top_classes:
        Tuple/sequence of integer class values treated as "top-class" (seed
        cells).  Default ``(5,)`` -- only the highest class.
    min_area_km2:
        Minimum area threshold in km2.  Regions smaller than this are
        discarded.  Default ``0.5`` km2 (approx. minimum viable utility-scale
        PV footprint).
    max_site_area_km2:
        Maximum area for a single candidate site in km2.  A connected component
        larger than this is subdivided into a grid of realistically-sized
        sub-sites (A2 fix) so that an entire high-LSI region is never presented
        as one decision.  ``None`` (default) disables subdivision.
    distance_to_ptl:
        Optional DataArray of distances (metres) to the nearest
        power-transmission line.  When provided, ``mean_ptl_dist`` is
        computed and included as a ranking tiebreaker (ascending -- closer is
        better).  Shape must match ``lsi``.
    top_k:
        Maximum number of candidate sites to return after ranking.
        Default 10.
    simplify_tolerance_m:
        Douglas-Peucker simplification tolerance in metres (CRS units).
        ``0.0`` (default) means no simplification.

    Returns
    -------
    geopandas.GeoDataFrame
        Ranked candidate sites.  Columns: see :data:`SITE_COLUMNS` plus
        ``geometry`` (polygon in the working CRS).  CRS matches the input
        rasters.  ``rank=1`` is the best site.  Empty GeoDataFrame (correct
        schema + CRS) if no region passes the minimum-area filter.

    Notes
    -----
    **Connectivity**: 8-connectivity (queen's case) -- diagonally adjacent
    cells are considered contiguous.  This is the most inclusive option and
    avoids splitting geometrically coherent patches that touch only at
    corners.

    **Ranking composite** (applied in order):

    1. ``mean_lsi`` descending
    2. ``area_km2`` descending
    3. ``mean_ptl_dist`` ascending (if ``distance_to_ptl`` provided;
       otherwise ``site_id`` ascending as deterministic tiebreaker)
    """
    # ------------------------------------------------------------------
    # 0. Validate inputs
    # ------------------------------------------------------------------
    if lsi.shape != class_raster.shape:
        raise ValueError(f"lsi.shape {lsi.shape} != class_raster.shape {class_raster.shape}")
    if distance_to_ptl is not None and distance_to_ptl.shape != lsi.shape:
        raise ValueError(f"distance_to_ptl.shape {distance_to_ptl.shape} != lsi.shape {lsi.shape}")

    transform = _get_transform(class_raster)
    cell_area_km2_val = _cell_area_km2(transform)
    crs_string = _crs_to_epsg_string(class_raster)

    # ------------------------------------------------------------------
    # 1. Build binary top-class mask (bool array)
    # ------------------------------------------------------------------
    cls_arr = class_raster.values  # shape (height, width)
    top_mask = np.zeros(cls_arr.shape, dtype=bool)
    for tc in top_classes:
        top_mask |= cls_arr == tc

    # ------------------------------------------------------------------
    # 2. Connected components with 8-connectivity
    # ------------------------------------------------------------------
    _label_out = cast(
        "tuple[np.ndarray, int]",
        ndimage_label(top_mask, structure=_CONNECTIVITY_8),
    )
    labeled_arr: np.ndarray = _label_out[0]
    num_features: int = int(_label_out[1])
    # labeled_arr: int array, 0 = background, 1..num_features = regions

    # A2: subdivide oversized regions so a whole high-LSI area is not presented
    # as one "site". Preserves total area; no-op when max_site_area_km2 is None.
    labeled_arr, num_features = _split_large_regions(
        labeled_arr, num_features, cell_area_km2_val, max_site_area_km2
    )

    # ------------------------------------------------------------------
    # 3. Helper: empty GeoDataFrame with correct schema
    # ------------------------------------------------------------------
    def _empty_gdf() -> gpd.GeoDataFrame:
        gdf = gpd.GeoDataFrame(
            {col: [] for col in SITE_COLUMNS},
            geometry=[],
            crs=crs_string,
        )
        # Enforce numeric dtypes on empty frame
        for col in ["site_id", "rank"]:
            gdf[col] = gdf[col].astype(int)
        for col in [
            "area_km2",
            "mean_lsi",
            "max_lsi",
            "centroid_x",
            "centroid_y",
            "centroid_lon",
            "centroid_lat",
            "mean_ptl_dist",
        ]:
            gdf[col] = gdf[col].astype(float)
        return gdf

    if num_features == 0:
        return _empty_gdf()

    # ------------------------------------------------------------------
    # 4. Filter by minimum area + collect per-region stats
    # ------------------------------------------------------------------
    lsi_arr = lsi.values.astype(float)
    ptl_arr = distance_to_ptl.values.astype(float) if distance_to_ptl is not None else None

    # WGS-84 transformer for centroid lon/lat
    wgs84_transformer: Transformer | None = None
    if crs_string is not None:
        try:
            wgs84_transformer = Transformer.from_crs(crs_string, "EPSG:4326", always_xy=True)
        except Exception:  # broad catch: rioxarray may raise many error types
            warnings.warn(
                "extract_sites: could not build WGS-84 transformer from "
                f"'{crs_string}'; centroid_lon/centroid_lat will be NaN.",
                UserWarning,
                stacklevel=2,
            )

    records: list[dict[str, object]] = []

    for label_id in range(1, num_features + 1):
        region_mask = labeled_arr == label_id  # bool (H, W)
        cell_count = int(region_mask.sum())
        area = cell_count * cell_area_km2_val

        if area < min_area_km2:
            continue  # below minimum-area threshold

        # --- LSI stats ---
        lsi_vals_in_region = lsi_arr[region_mask]
        valid_lsi = lsi_vals_in_region[~np.isnan(lsi_vals_in_region)]
        mean_lsi = float(valid_lsi.mean()) if valid_lsi.size > 0 else math.nan
        max_lsi = float(valid_lsi.max()) if valid_lsi.size > 0 else math.nan

        # --- PTL distance stats ---
        if ptl_arr is not None:
            ptl_vals_in_region = ptl_arr[region_mask]
            valid_ptl = ptl_vals_in_region[~np.isnan(ptl_vals_in_region)]
            mean_ptl_dist: float = float(valid_ptl.mean()) if valid_ptl.size > 0 else math.nan
        else:
            mean_ptl_dist = math.nan

        # --- Polygonization ---
        # Build a uint8 mask for this region; rasterio_shapes needs uint8
        region_uint8 = region_mask.astype(np.uint8)
        geoms = [
            shapely_shape(geom_dict)
            for geom_dict, val in rasterio_shapes(region_uint8, transform=transform)
            if int(val) == 1
        ]
        if not geoms:
            # Degenerate region produced no shapes; skip
            continue
        polygon = unary_union(geoms)
        if simplify_tolerance_m > 0.0:
            polygon = polygon.simplify(simplify_tolerance_m, preserve_topology=True)

        # --- Centroid ---
        centroid = polygon.centroid
        cx = float(centroid.x)
        cy = float(centroid.y)

        if wgs84_transformer is not None:
            try:
                lon, lat = wgs84_transformer.transform(cx, cy)
                centroid_lon: float = float(lon)
                centroid_lat: float = float(lat)
            except Exception:  # broad catch: rioxarray may raise many error types
                centroid_lon = math.nan
                centroid_lat = math.nan
        else:
            centroid_lon = math.nan
            centroid_lat = math.nan

        records.append(
            {
                "site_id": label_id,
                "area_km2": area,
                "mean_lsi": mean_lsi,
                "max_lsi": max_lsi,
                "centroid_x": cx,
                "centroid_y": cy,
                "centroid_lon": centroid_lon,
                "centroid_lat": centroid_lat,
                "mean_ptl_dist": mean_ptl_dist,
                "geometry": polygon,
            }
        )

    if not records:
        return _empty_gdf()

    # ------------------------------------------------------------------
    # 5. Build GeoDataFrame
    # ------------------------------------------------------------------
    gdf = gpd.GeoDataFrame(records, crs=crs_string)

    # ------------------------------------------------------------------
    # 6. Ranking composite
    # ------------------------------------------------------------------
    # Sort keys (pandas sort_values puts NaN last by default):
    #   Key 1: mean_lsi descending   (higher suitability is better)
    #   Key 2: area_km2 descending   (larger footprint is better)
    #   Key 3a (with PTL): mean_ptl_dist ascending  (closer to grid is better)
    #   Key 3b (no PTL):   site_id ascending         (deterministic tiebreaker)
    has_ptl = ptl_arr is not None

    if has_ptl:
        sort_cols: list[str] = ["mean_lsi", "area_km2", "mean_ptl_dist", "site_id"]
        ascending: list[bool] = [False, False, True, True]
    else:
        sort_cols = ["mean_lsi", "area_km2", "site_id"]
        ascending = [False, False, True]

    gdf = gdf.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)
    gdf["rank"] = np.arange(1, len(gdf) + 1, dtype=int)

    # ------------------------------------------------------------------
    # 7. Top-k truncation
    # ------------------------------------------------------------------
    gdf = gdf.head(top_k).copy()

    # Ensure canonical column order (cast needed: __getitem__ on GeoDataFrame returns DataFrame)
    gdf = cast(gpd.GeoDataFrame, gdf[[*SITE_COLUMNS, "geometry"]])

    return gdf
