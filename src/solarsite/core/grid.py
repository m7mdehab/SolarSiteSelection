"""Raster grid specification and empty DataArray factory.

GridSpec captures the spatial extent (in a projected CRS), resolution, and
derived width/height.  It is the canonical descriptor of any raster layer
in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr
from pyproj import CRS
from rasterio.transform import from_bounds

__all__ = [
    "GridSpec",
    "empty_dataarray",
]

DEFAULT_RESOLUTION_M: int = 100  # metres


@dataclass(frozen=True)
class GridSpec:
    """Immutable raster grid specification.

    All spatial quantities are in the native CRS units (metres for UTM/equal-area).

    Attributes:
        minx: Western boundary (CRS units).
        miny: Southern boundary (CRS units).
        maxx: Eastern boundary (CRS units).
        maxy: Northern boundary (CRS units).
        resolution_m: Cell size in metres (default 100).
        crs: The coordinate reference system for this grid.
        width: Number of columns (derived).
        height: Number of rows (derived).
        transform: Rasterio/affine transform for the grid (derived).
    """

    minx: float
    miny: float
    maxx: float
    maxy: float
    resolution_m: int = DEFAULT_RESOLUTION_M
    crs: CRS = field(default_factory=lambda: CRS.from_epsg(32636))

    def __post_init__(self) -> None:
        if self.minx >= self.maxx:
            raise ValueError(f"minx ({self.minx}) must be < maxx ({self.maxx})")
        if self.miny >= self.maxy:
            raise ValueError(f"miny ({self.miny}) must be < maxy ({self.maxy})")
        if self.resolution_m <= 0:
            raise ValueError(f"resolution_m must be positive, got {self.resolution_m}")

    @property
    def width(self) -> int:
        """Number of columns."""
        return max(1, int(np.ceil((self.maxx - self.minx) / self.resolution_m)))

    @property
    def height(self) -> int:
        """Number of rows."""
        return max(1, int(np.ceil((self.maxy - self.miny) / self.resolution_m)))

    @property
    def transform(self) -> Any:
        """Rasterio affine transform: pixel (0,0) is top-left corner of (minx, maxy)."""
        return from_bounds(self.minx, self.miny, self.maxx, self.maxy, self.width, self.height)

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(
            (self.minx, self.miny, self.maxx, self.maxy, self.resolution_m, self.crs.to_epsg())
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GridSpec):
            return NotImplemented
        return (
            self.minx == other.minx
            and self.miny == other.miny
            and self.maxx == other.maxx
            and self.maxy == other.maxy
            and self.resolution_m == other.resolution_m
            and self.crs == other.crs
        )


def empty_dataarray(
    spec: GridSpec, name: str = "data", fill_value: float = float("nan")
) -> xr.DataArray:
    """Create an empty (NaN-filled) DataArray aligned to a GridSpec.

    The DataArray uses (y, x) dimensions with coordinate arrays matching the
    cell-centre positions in the GridSpec CRS.

    Args:
        spec: The raster grid specification.
        name: Variable name for the DataArray.
        fill_value: Fill value (default NaN).

    Returns:
        An xarray DataArray of shape (height, width) with spatial metadata set.
    """
    # Cell-centre coordinates
    x_coords = spec.minx + (np.arange(spec.width) + 0.5) * spec.resolution_m
    y_coords = spec.maxy - (np.arange(spec.height) + 0.5) * spec.resolution_m

    data = np.full((spec.height, spec.width), fill_value, dtype=np.float64)

    da = xr.DataArray(
        data,
        dims=["y", "x"],
        coords={"y": y_coords, "x": x_coords},
        name=name,
    )

    # Attach CRS and spatial_ref via rioxarray conventions
    da = da.rio.write_crs(spec.crs, inplace=True)
    da = da.rio.write_transform(spec.transform, inplace=True)

    return da
