"""Tests for proximity raster computation (src/solarsite/core/proximity.py).

The acceptance criterion is exact:
    - Cell at the feature = 0 m
    - Adjacent cell (4-connected) = 100 m  (1 cell x 100 m/cell)
    - Diagonal cell = ~141.42 m  (sqrt(2) x 100 m)

We use a 5x5 grid at 100 m resolution with a point feature at the centre cell.
"""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pytest
from pyproj import CRS
from shapely.geometry import Point

from solarsite.core.grid import GridSpec
from solarsite.core.proximity import proximity_raster

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTM36N = CRS.from_epsg(32636)  # UTM zone 36N (Egypt)

# A 5x5 grid at 100 m resolution: 500 m x 500 m extent
# Origin at (0, 0) in the projected CRS so hand-computation is trivial
_SPEC_5X5 = GridSpec(
    minx=0.0,
    miny=0.0,
    maxx=500.0,
    maxy=500.0,
    resolution_m=100,
    crs=_UTM36N,
)


def _feature_at_cell(row: int, col: int, spec: GridSpec = _SPEC_5X5) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame with a single Point at the centre of cell (row, col).

    Row 0 is the topmost row (highest y), following raster conventions.
    Col 0 is the leftmost column (lowest x).
    """
    x = spec.minx + (col + 0.5) * spec.resolution_m
    y = spec.maxy - (row + 0.5) * spec.resolution_m
    pt = Point(x, y)
    return gpd.GeoDataFrame(geometry=[pt], crs=spec.crs)


# ---------------------------------------------------------------------------
# Core acceptance tests
# ---------------------------------------------------------------------------


def test_feature_cell_has_zero_distance() -> None:
    """The cell that contains the feature must have distance = 0."""
    row, col = 2, 2  # centre of 5x5 grid
    gdf = _feature_at_cell(row, col)
    da = proximity_raster(gdf, _SPEC_5X5)
    assert da.values[row, col] == pytest.approx(0.0, abs=1e-6)


def test_adjacent_cell_is_one_cell_away() -> None:
    """A 4-connected neighbour is exactly 1 cell = 100 m away."""
    row, col = 2, 2
    gdf = _feature_at_cell(row, col)
    da = proximity_raster(gdf, _SPEC_5X5)
    # Right neighbour
    assert da.values[row, col + 1] == pytest.approx(100.0, abs=1e-3)
    # Left neighbour
    assert da.values[row, col - 1] == pytest.approx(100.0, abs=1e-3)
    # Up neighbour (row - 1 = row above in raster convention)
    assert da.values[row - 1, col] == pytest.approx(100.0, abs=1e-3)
    # Down neighbour
    assert da.values[row + 1, col] == pytest.approx(100.0, abs=1e-3)


def test_diagonal_cell_is_sqrt2_cells_away() -> None:
    """A diagonal neighbour is √2 cells ≈ 141.42 m away."""
    row, col = 2, 2
    gdf = _feature_at_cell(row, col)
    da = proximity_raster(gdf, _SPEC_5X5)
    expected = math.sqrt(2) * 100.0  # ≈ 141.421 m
    assert da.values[row - 1, col + 1] == pytest.approx(expected, abs=1e-3)
    assert da.values[row + 1, col - 1] == pytest.approx(expected, abs=1e-3)
    assert da.values[row - 1, col - 1] == pytest.approx(expected, abs=1e-3)
    assert da.values[row + 1, col + 1] == pytest.approx(expected, abs=1e-3)


def test_two_cell_distance() -> None:
    """A cell 2 steps away should be 200 m."""
    row, col = 2, 2
    gdf = _feature_at_cell(row, col)
    da = proximity_raster(gdf, _SPEC_5X5)
    assert da.values[row, col + 2] == pytest.approx(200.0, abs=1e-3)
    assert da.values[row + 2, col] == pytest.approx(200.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Hand-computed fixture with corner feature
# ---------------------------------------------------------------------------


def test_corner_feature_full_grid_distances() -> None:
    """Feature at top-left corner (0,0); verify selected distances in 5x5 grid."""
    row, col = 0, 0
    gdf = _feature_at_cell(row, col)
    da = proximity_raster(gdf, _SPEC_5X5)

    assert da.values[0, 0] == pytest.approx(0.0, abs=1e-6)
    assert da.values[0, 1] == pytest.approx(100.0, abs=1e-3)
    assert da.values[1, 0] == pytest.approx(100.0, abs=1e-3)
    assert da.values[1, 1] == pytest.approx(math.sqrt(2) * 100.0, abs=1e-3)
    assert da.values[0, 4] == pytest.approx(400.0, abs=1e-3)
    assert da.values[4, 4] == pytest.approx(math.sqrt(2) * 400.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Shape and dtype
# ---------------------------------------------------------------------------


def test_output_shape_matches_grid() -> None:
    gdf = _feature_at_cell(2, 2)
    da = proximity_raster(gdf, _SPEC_5X5)
    assert da.shape == (_SPEC_5X5.height, _SPEC_5X5.width)
    assert da.shape == (5, 5)


def test_output_dtype_is_float() -> None:
    gdf = _feature_at_cell(2, 2)
    da = proximity_raster(gdf, _SPEC_5X5)
    assert np.issubdtype(da.dtype, np.floating)


def test_all_values_nonnegative() -> None:
    gdf = _feature_at_cell(1, 3)
    da = proximity_raster(gdf, _SPEC_5X5)
    assert np.all(da.values >= 0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_features_returns_nan_grid() -> None:
    """Empty GeoDataFrame → all cells are NaN (no feature to measure from)."""
    empty = gpd.GeoDataFrame(geometry=[], crs=_UTM36N)
    da = proximity_raster(empty, _SPEC_5X5)
    assert da.shape == (5, 5)
    assert np.all(np.isnan(da.values))


def test_different_resolution() -> None:
    """Proximity raster scales correctly with a 50 m resolution grid."""
    spec_50m = GridSpec(
        minx=0.0,
        miny=0.0,
        maxx=500.0,
        maxy=500.0,
        resolution_m=50,
        crs=_UTM36N,
    )
    # Feature at centre of the 10x10 grid (row 5, col 5)
    row, col = 5, 5
    x = spec_50m.minx + (col + 0.5) * spec_50m.resolution_m
    y = spec_50m.maxy - (row + 0.5) * spec_50m.resolution_m
    gdf = gpd.GeoDataFrame(geometry=[Point(x, y)], crs=spec_50m.crs)
    da = proximity_raster(gdf, spec_50m)
    assert da.values[row, col] == pytest.approx(0.0, abs=1e-6)
    assert da.values[row, col + 1] == pytest.approx(50.0, abs=1e-3)
    assert da.values[row + 1, col + 1] == pytest.approx(math.sqrt(2) * 50.0, abs=1e-3)
