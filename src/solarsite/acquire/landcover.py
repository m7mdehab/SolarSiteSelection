"""Land cover and protected-area exclusion sources.

This module provides:

* :class:`WorldCoverSource` -- ESA WorldCover 2021 v200 (10 m) from the public
  AWS bucket, resampled to the analysis grid using **nearest-neighbour** so
  integer class codes are never averaged.

* :class:`WDPASource` -- World Database on Protected Areas, read from a LOCAL
  GeoPackage ``data/manual/wdpa_egypt.gpkg``.  If the file is absent the
  source returns an empty GeoDataFrame and sets ``available = False``; it
  never crashes (H2 fallback).

* :func:`exclusion_mask` -- combines WorldCover classes 50 (built-up) and 80
  (permanent water body) with WDPA polygons into a boolean exclusion raster
  aligned to the analysis grid (``True`` = excluded).

Tile-naming convention
----------------------
ESA WorldCover v200 tiles are 3 x 3 degrees and named after their lower-left
corner in the form ``<NS><lat2><EW><lon3>``, e.g. ``N30E027`` (30 N, 27 E).
Latitude digits are zero-padded to 2, longitude to 3.

The public AWS base URL is::

    https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/
    ESA_WorldCover_10m_2021_v200_{TILE}_Map.tif

Access is anonymous (no credentials required).
"""

from __future__ import annotations

import io
import logging
import math
import warnings
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import numpy as np
import rasterio
import rasterio.features
import rasterio.transform
import rasterio.warp
import rioxarray  # noqa: F401 -- registers .rio accessor
import xarray as xr
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge
from shapely.geometry import box

from solarsite.core import AOI, DiskCache, working_crs_for

from .base import AcquisitionError, RasterSource, VectorSource, grid_for_aoi, request_with_retry

__all__ = [
    "WDPASource",
    "WorldCoverSource",
    "exclusion_mask",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WC_BASE = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
    "/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

# ESA WorldCover v2 hard-exclusion classes (built-up=50, water=80).
_HARD_EXCLUSION_CLASSES: frozenset[int] = frozenset({50, 80})

# Default path to the local WDPA GeoPackage (relative to the project root).
_DEFAULT_WDPA_PATH = Path("data/manual/wdpa_egypt.gpkg")


# ---------------------------------------------------------------------------
# Tile helpers
# ---------------------------------------------------------------------------


def _wc_tile_name(lat_ll: int, lon_ll: int) -> str:
    """Return the ESA WorldCover tile name for a lower-left corner (integer degrees).

    The convention is: <N|S><lat2><E|W><lon3>.
    E.g. lat_ll=30, lon_ll=27  ->  ``N30E027``.
    """
    ns = "N" if lat_ll >= 0 else "S"
    ew = "E" if lon_ll >= 0 else "W"
    return f"{ns}{abs(lat_ll):02d}{ew}{abs(lon_ll):03d}"


def _covering_tiles(minx: float, miny: float, maxx: float, maxy: float) -> list[tuple[int, int]]:
    """Return all (lat_ll, lon_ll) pairs of 3x3-degree tiles that cover the bbox.

    The bbox coordinates are in WGS-84 (degrees).  The function floors to the
    nearest multiple of 3 degrees to find the lower-left corners.
    """
    lat0 = math.floor(miny / 3) * 3
    lon0 = math.floor(minx / 3) * 3
    tiles = []
    lat = lat0
    while lat < maxy:
        lon = lon0
        while lon < maxx:
            tiles.append((lat, lon))
            lon += 3
        lat += 3
    return tiles


# ---------------------------------------------------------------------------
# WorldCoverSource
# ---------------------------------------------------------------------------


class WorldCoverSource(RasterSource):
    """ESA WorldCover 2021 v200 (10 m) from the public AWS bucket.

    Returns an :class:`xarray.DataArray` of **integer class codes** (10..100)
    resampled with **nearest-neighbour** to the analysis grid.  Nearest-
    neighbour preserves the categorical codes -- averaging would produce
    nonsense values.

    Parameters
    ----------
    cache:
        Optional :class:`~solarsite.core.DiskCache`.  Defaults to a cache in
        the platform temp directory.
    """

    name = "worldcover"

    def _fetch_uncached(self, aoi: AOI, resolution_m: int = 100, **params: Any) -> xr.DataArray:
        """Download tiles, mosaic, reproject (nearest), clip to AOI grid."""
        grid = grid_for_aoi(aoi, resolution_m)
        wgs84_bounds = aoi.bounds  # (minx, miny, maxx, maxy) in WGS-84

        tiles = _covering_tiles(*wgs84_bounds)
        if not tiles:
            raise AcquisitionError("No WorldCover tiles cover the AOI bounds.")

        logger.debug("WorldCover: fetching %d tile(s) for AOI %s", len(tiles), aoi.hash[:8])

        tile_datasets: list[rasterio.DatasetReader] = []
        memfiles: list[MemoryFile] = []

        try:
            with httpx.Client(timeout=120.0) as client:
                for lat_ll, lon_ll in tiles:
                    tile = _wc_tile_name(lat_ll, lon_ll)
                    url = _WC_BASE.format(tile=tile)
                    logger.debug("WorldCover: GET %s", url)
                    resp = request_with_retry(client, "GET", url)
                    if resp.status_code == 404:
                        # Some ocean tiles don't exist -- skip gracefully.
                        logger.debug("WorldCover: tile %s not found (ocean?), skipping.", tile)
                        continue
                    if resp.status_code != 200:
                        raise AcquisitionError(f"WorldCover tile {tile}: HTTP {resp.status_code}")
                    mf = MemoryFile(io.BytesIO(resp.content))
                    memfiles.append(mf)
                    tile_datasets.append(mf.open())

            if not tile_datasets:
                raise AcquisitionError("All WorldCover tiles were missing (ocean coverage?).")

            return _mosaic_and_reproject(tile_datasets, grid)

        finally:
            for ds in tile_datasets:
                ds.close()
            for mf in memfiles:
                mf.close()


def _mosaic_and_reproject(datasets: list[rasterio.DatasetReader], grid: Any) -> xr.DataArray:
    """Merge tile datasets, then reproject to *grid* using nearest-neighbour.

    Nearest-neighbour is mandatory for categorical land-cover codes so that
    class values are never interpolated/averaged.

    Returns an xarray.DataArray with dtype=uint8, CRS=grid.crs.
    """
    # Merge (mosaic) the raw tile rasters -- still in WGS-84 / geographic CRS.
    merged_data, merged_transform = merge(datasets, method="first")
    src_crs = datasets[0].crs
    nodata = datasets[0].nodata

    # Build a temporary in-memory rasterio dataset for the mosaic.
    with MemoryFile() as mf:
        with mf.open(
            driver="GTiff",
            count=1,
            dtype=merged_data.dtype,
            crs=src_crs,
            transform=merged_transform,
            width=merged_data.shape[2],
            height=merged_data.shape[1],
            nodata=nodata,
        ) as mosaic_ds:
            mosaic_ds.write(merged_data)

        with mf.open() as mosaic_ds:
            # Reproject to the target grid using nearest-neighbour.
            out_data = np.zeros((grid.height, grid.width), dtype=np.uint8)
            rasterio.warp.reproject(
                source=mosaic_ds.read(1),
                destination=out_data,
                src_transform=mosaic_ds.transform,
                src_crs=mosaic_ds.crs,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                resampling=Resampling.nearest,
                src_nodata=nodata,
                dst_nodata=0,
            )

    # Wrap in an xarray DataArray with spatial metadata.
    x_coords = grid.minx + (np.arange(grid.width) + 0.5) * grid.resolution_m
    y_coords = grid.maxy - (np.arange(grid.height) + 0.5) * grid.resolution_m

    da = xr.DataArray(
        out_data,
        dims=["y", "x"],
        coords={"y": y_coords, "x": x_coords},
        name="worldcover",
    )
    da = da.rio.write_crs(grid.crs, inplace=True)
    da = da.rio.write_transform(grid.transform, inplace=True)
    return da


# ---------------------------------------------------------------------------
# WDPASource
# ---------------------------------------------------------------------------


class WDPASource(VectorSource):
    """Local WDPA protected-area polygons.

    Reads ``data/manual/wdpa_egypt.gpkg`` (relative to the working directory,
    or the path supplied at construction time).  The file is **not** downloaded
    -- it must be placed there manually (H2 requirement).

    If the file is absent:

    * ``available`` is set to ``False``,
    * a warning is emitted,
    * an empty GeoDataFrame in the working CRS is returned -- the pipeline
      continues without protected-area exclusions.

    Parameters
    ----------
    wdpa_path:
        Override the default path to the WDPA GeoPackage.
    cache:
        Optional :class:`~solarsite.core.DiskCache`.
    """

    name = "wdpa"

    def __init__(
        self,
        wdpa_path: Path | str | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        super().__init__(cache=cache)
        self._wdpa_path: Path = Path(wdpa_path) if wdpa_path is not None else _DEFAULT_WDPA_PATH
        self.available: bool = self._wdpa_path.exists()
        if not self.available:
            warnings.warn(
                f"WDPA file not found at '{self._wdpa_path}'. "
                "Protected-area exclusions will be skipped. "
                "Place the GeoPackage there to enable them.",
                UserWarning,
                stacklevel=2,
            )

    def _fetch_uncached(self, aoi: AOI, resolution_m: int = 100, **params: Any) -> gpd.GeoDataFrame:
        """Read WDPA polygons, clip to AOI, reproject to working CRS."""
        crs = working_crs_for(aoi.geometry)

        if not self.available:
            logger.debug("WDPASource: file absent, returning empty GeoDataFrame.")
            return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs), crs=crs)

        gdf: gpd.GeoDataFrame = gpd.read_file(self._wdpa_path)

        # Clip to AOI bounding box in WGS-84 before reprojecting (cheaper).
        minx, miny, maxx, maxy = aoi.bounds
        aoi_box = box(minx, miny, maxx, maxy)
        # Ensure source is in WGS-84 for the clip.
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        clipped: gpd.GeoDataFrame = gdf.loc[gdf.geometry.intersects(aoi_box)].copy()  # type: ignore[assignment]

        if clipped.empty:
            return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=crs), crs=crs)

        # Reproject to working (UTM) CRS.
        return clipped.to_crs(crs)


# ---------------------------------------------------------------------------
# exclusion_mask
# ---------------------------------------------------------------------------


def exclusion_mask(
    aoi: AOI,
    resolution_m: int = 100,
    worldcover_source: WorldCoverSource | None = None,
    wdpa_source: WDPASource | None = None,
) -> xr.DataArray:
    """Build a boolean exclusion mask aligned to the analysis grid.

    A cell is **True** (excluded) when it satisfies ANY of:

    * Its ESA WorldCover class is 50 (built-up) or 80 (permanent water body).
    * It intersects a WDPA protected-area polygon.

    Parameters
    ----------
    aoi:
        The area of interest.
    resolution_m:
        Cell size in metres (default 100).
    worldcover_source:
        Optional pre-constructed :class:`WorldCoverSource`.  Created with
        default settings if omitted.
    wdpa_source:
        Optional pre-constructed :class:`WDPASource`.  Created with default
        settings if omitted.

    Returns
    -------
    xr.DataArray
        Boolean (uint8, 0/1) DataArray aligned to the AOI grid.  ``1`` means
        excluded, ``0`` means potentially suitable.
    """
    if worldcover_source is None:
        worldcover_source = WorldCoverSource()
    if wdpa_source is None:
        wdpa_source = WDPASource()

    grid = grid_for_aoi(aoi, resolution_m)

    # --- WorldCover hard exclusions (classes 50 and 80) ---
    lc: xr.DataArray = worldcover_source.fetch(aoi, resolution_m)
    lc_arr = lc.values.astype(np.uint8)

    excl = np.zeros((grid.height, grid.width), dtype=np.uint8)
    for cls in _HARD_EXCLUSION_CLASSES:
        excl |= (lc_arr == cls).astype(np.uint8)

    # --- WDPA rasterization ---
    wdpa_gdf: gpd.GeoDataFrame = wdpa_source.fetch(aoi, resolution_m)

    if not wdpa_gdf.empty:
        shapes = [(geom, 1) for geom in wdpa_gdf.geometry if geom is not None and not geom.is_empty]
        if shapes:
            wdpa_raster: np.ndarray[tuple[int, int], np.dtype[np.uint8]] = (
                rasterio.features.rasterize(
                    shapes=shapes,
                    out_shape=(grid.height, grid.width),
                    transform=grid.transform,
                    fill=0,
                    dtype=np.uint8,
                )
            )
            excl |= wdpa_raster  # type: ignore[operator]

    # Wrap in a DataArray with spatial metadata.
    x_coords = grid.minx + (np.arange(grid.width) + 0.5) * grid.resolution_m
    y_coords = grid.maxy - (np.arange(grid.height) + 0.5) * grid.resolution_m

    mask_da = xr.DataArray(
        excl,
        dims=["y", "x"],
        coords={"y": y_coords, "x": x_coords},
        name="exclusion_mask",
    )
    mask_da = mask_da.rio.write_crs(grid.crs, inplace=True)
    mask_da = mask_da.rio.write_transform(grid.transform, inplace=True)
    return mask_da
