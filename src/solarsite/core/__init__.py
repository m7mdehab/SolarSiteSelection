"""Shared geospatial kernel: AOI model, CRS policy, grid, cache, proximity rasters."""

from .aoi import AOI, AOIInvalidGeometryError, AOITooLargeError
from .cache import CacheKey, DiskCache
from .crs import AREA_CRS, WGS84, utm_zone_for, working_crs_for
from .grid import DEFAULT_RESOLUTION_M, GridSpec, empty_dataarray
from .proximity import proximity_raster
from .raster import align_to_grid, reproject_to_grid, round_trip_reproject

__all__ = [
    # aoi
    "AOI",
    "AREA_CRS",
    "DEFAULT_RESOLUTION_M",
    "WGS84",
    "AOIInvalidGeometryError",
    "AOITooLargeError",
    "CacheKey",
    # cache
    "DiskCache",
    # grid
    "GridSpec",
    "align_to_grid",
    "empty_dataarray",
    # proximity
    "proximity_raster",
    # raster
    "reproject_to_grid",
    "round_trip_reproject",
    "utm_zone_for",
    # crs
    "working_crs_for",
]
