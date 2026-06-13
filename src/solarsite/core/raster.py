"""Raster reproject/align/resample utilities using rioxarray and rasterio.

All operations assume DataArrays carry a spatial_ref / CRS written by rioxarray.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import xarray as xr
from pyproj import CRS, Transformer
from rasterio.enums import Resampling

from .grid import GridSpec, empty_dataarray

__all__ = [
    "align_to_grid",
    "reproject_to_grid",
    "round_trip_reproject",
]

ResamplingMethod = Literal["nearest", "bilinear", "cubic", "average"]

_RASTERIO_RESAMPLING: dict[str, Resampling] = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "average": Resampling.average,
}


def reproject_to_grid(
    da: xr.DataArray,
    spec: GridSpec,
    resampling: ResamplingMethod = "bilinear",
) -> xr.DataArray:
    """Reproject and resample a DataArray to match a GridSpec.

    The input DataArray must have a CRS set via rioxarray (``rio.crs``).

    Args:
        da: Source DataArray with spatial metadata.
        spec: Target grid specification (CRS, extent, resolution).
        resampling: Resampling algorithm.

    Returns:
        A new DataArray reprojected and clipped to the GridSpec extent.
    """
    rs = _RASTERIO_RESAMPLING.get(resampling)
    if rs is None:
        raise ValueError(f"Unknown resampling method '{resampling}'.")

    # rio.reproject_match requires another DataArray or CRS + shape
    layer_name: str = str(da.name) if da.name is not None else "data"
    target = empty_dataarray(spec, name=layer_name, fill_value=float("nan"))

    reprojected: xr.DataArray = da.rio.reproject_match(target, resampling=rs)
    return reprojected


def align_to_grid(
    da: xr.DataArray,
    spec: GridSpec,
    resampling: ResamplingMethod = "bilinear",
) -> xr.DataArray:
    """Alias for reproject_to_grid for semantic clarity in alignment contexts."""
    return reproject_to_grid(da, spec, resampling=resampling)


def round_trip_reproject(
    da: xr.DataArray,
    intermediate_crs: CRS,
) -> xr.DataArray:
    """Reproject a DataArray to an intermediate CRS and back.

    Used for testing: the reprojection error in a round-trip should be less
    than one cell size.

    Args:
        da: Source DataArray with spatial metadata.
        intermediate_crs: CRS to project to before projecting back.

    Returns:
        A DataArray in the original CRS, reprojected via intermediate_crs.
    """
    original_crs = da.rio.crs
    if original_crs is None:
        raise ValueError("DataArray has no CRS; set it with da.rio.write_crs() first.")

    # Forward: source → intermediate
    da_fwd: xr.DataArray = da.rio.reproject(intermediate_crs, resampling=Resampling.bilinear)

    # Backward: intermediate → source CRS
    da_back: xr.DataArray = da_fwd.rio.reproject(original_crs, resampling=Resampling.bilinear)

    return da_back


def _pixel_coords_to_geographic(
    da: xr.DataArray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (lon, lat) arrays for all pixels in the DataArray (source CRS → WGS-84).

    Used internally for reprojection validation.
    """
    src_crs = da.rio.crs
    if src_crs is None:
        raise ValueError("DataArray has no CRS.")

    transformer = Transformer.from_crs(src_crs, CRS.from_epsg(4326), always_xy=True)
    xs = da.coords["x"].values
    ys = da.coords["y"].values
    xx, yy = np.meshgrid(xs, ys)
    lons, lats = transformer.transform(xx, yy)  # type: ignore[call-overload]
    return lons, lats
