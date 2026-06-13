"""Proximity (Euclidean distance) raster computation.

Given a GeoDataFrame of vector features (points, lines, or polygons) and a
GridSpec, compute per-cell Euclidean distance in metres to the nearest feature.

Algorithm
---------
1. Rasterize the features onto the grid (burned pixels = 0, background = 1).
2. Run ``scipy.ndimage.distance_transform_edt`` which gives the EDT in *pixel*
   units for the background cells.
3. Multiply by ``resolution_m`` to convert pixel distances to metres.

This is correct because EDT measures distance in isotropic pixel units, and
one pixel = ``resolution_m`` metres in our equal-area / UTM grid.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import xarray as xr
from rasterio.features import rasterize
from scipy.ndimage import distance_transform_edt

from .grid import GridSpec

__all__ = ["proximity_raster"]


def proximity_raster(
    features: gpd.GeoDataFrame,
    spec: GridSpec,
    *,
    nodata: float = float("nan"),
) -> xr.DataArray:
    """Compute a Euclidean distance raster from vector features.

    All features are burned as mask = 1; background = 0.  EDT is run on the
    background (inverted), then scaled by cell size to metres.

    Args:
        features: GeoDataFrame in the **same CRS as spec** (no reprojection here).
        spec: Target raster grid specification.
        nodata: Value to fill masked/no-data cells (default NaN; not used here
                since all cells get a valid distance).

    Returns:
        xr.DataArray of shape (height, width) with distance in metres.
        Cells that coincide with a feature have distance 0.
    """
    if features.empty:
        # All cells are infinitely far from any feature — fill with NaN
        dist = np.full((spec.height, spec.width), nodata, dtype=np.float64)
    else:
        # Rasterize features: burned = True (feature present), background = False
        shapes = ((geom, 1) for geom in features.geometry if geom is not None and not geom.is_empty)

        mask = rasterize(
            shapes=shapes,
            out_shape=(spec.height, spec.width),
            transform=spec.transform,
            fill=0,
            dtype=np.uint8,
        )

        # EDT operates on the *background* (mask == 0).
        # distance_transform_edt returns pixel distance for background cells.
        # Feature cells get distance 0.
        background: np.ndarray = np.where(mask == 0, 1, 0).astype(np.uint8)
        edt_pixels: np.ndarray = distance_transform_edt(background)  # type: ignore[assignment]

        # Scale from pixel units to metres
        dist = edt_pixels * float(spec.resolution_m)

    # Build cell-centre coordinate arrays
    x_coords = spec.minx + (np.arange(spec.width) + 0.5) * spec.resolution_m
    y_coords = spec.maxy - (np.arange(spec.height) + 0.5) * spec.resolution_m

    da = xr.DataArray(
        dist,
        dims=["y", "x"],
        coords={"y": y_coords, "x": x_coords},
        name="distance_m",
    )
    da = da.rio.write_crs(spec.crs, inplace=True)
    da = da.rio.write_transform(spec.transform, inplace=True)
    return da
