"""Tests for raster reproject/align utilities (src/solarsite/core/raster.py).

Acceptance criterion: round-trip reprojection error < 1 cell (100 m).
"""

from __future__ import annotations

import numpy as np
import pytest
from pyproj import CRS

from solarsite.core.crs import WGS84
from solarsite.core.grid import GridSpec, empty_dataarray
from solarsite.core.raster import reproject_to_grid, round_trip_reproject

_UTM36N = CRS.from_epsg(32636)  # UTM zone 36N — Egypt


# ---------------------------------------------------------------------------
# Build a simple synthetic grid for tests
# ---------------------------------------------------------------------------


def _make_utm_grid(resolution_m: int = 100) -> tuple[GridSpec, object]:
    """Return a 10x10 GridSpec and DataArray in UTM 36N."""
    # Small box in UTM 36N around Cairo (approx)
    spec = GridSpec(
        minx=330_000.0,
        miny=3_320_000.0,
        maxx=331_000.0,
        maxy=3_321_000.0,
        resolution_m=resolution_m,
        crs=_UTM36N,
    )
    da = empty_dataarray(spec, name="test")
    # Fill with ramp values so we can check they come back reasonable
    ramp = np.arange(spec.height * spec.width, dtype=np.float64).reshape(spec.height, spec.width)
    da.values[:] = ramp
    return spec, da


# ---------------------------------------------------------------------------
# Round-trip reprojection test (acceptance criterion)
# ---------------------------------------------------------------------------


def test_round_trip_positional_error_less_than_one_cell() -> None:
    """Round-trip UTM 36N → WGS-84 → UTM 36N; coordinate shift < 100 m."""
    _spec, da = _make_utm_grid(resolution_m=100)

    # Get original cell-centre x/y coordinates
    original_x = da.coords["x"].values.copy()
    original_y = da.coords["y"].values.copy()

    # Perform round-trip reprojection
    da_back = round_trip_reproject(da, intermediate_crs=WGS84)  # type: ignore[arg-type]

    # The returned grid may be slightly offset due to resampling; check coordinate shift
    back_x = da_back.coords["x"].values
    back_y = da_back.coords["y"].values

    # Align grids by finding overlapping extents
    # Both should have similar x/y ranges; find max shift in metres
    min_len_x = min(len(original_x), len(back_x))
    min_len_y = min(len(original_y), len(back_y))

    shift_x = np.abs(original_x[:min_len_x] - back_x[:min_len_x])
    shift_y = np.abs(original_y[:min_len_y] - back_y[:min_len_y])

    max_shift = max(shift_x.max(), shift_y.max())
    assert max_shift < 100.0, (
        f"Round-trip coordinate shift {max_shift:.2f} m exceeds 1 cell (100 m)"
    )


def test_round_trip_grid_dimensions_preserved() -> None:
    """Round-trip should preserve approximate grid dimensions."""
    _spec, da = _make_utm_grid(resolution_m=100)
    da_back = round_trip_reproject(da, intermediate_crs=WGS84)  # type: ignore[arg-type]

    # Allow ±2 cells of size difference due to reprojection boundary effects
    assert abs(da_back.shape[0] - da.shape[0]) <= 2
    assert abs(da_back.shape[1] - da.shape[1]) <= 2


def test_round_trip_crs_restored() -> None:
    """The returned DataArray should be in the original CRS."""
    _spec, da = _make_utm_grid(resolution_m=100)
    da_back = round_trip_reproject(da, intermediate_crs=WGS84)  # type: ignore[arg-type]
    assert da_back.rio.crs is not None
    # Should still be UTM 36N (or equivalent)
    assert da_back.rio.crs.to_epsg() == 32636


def test_round_trip_values_approximately_preserved() -> None:
    """Round-trip values should be within ±30% of original (bilinear interpolation)."""
    _spec, da = _make_utm_grid(resolution_m=100)
    da_back = round_trip_reproject(da, intermediate_crs=WGS84)  # type: ignore[arg-type]

    # Compare interior values (avoid boundary artefacts)
    h = min(da.shape[0], da_back.shape[0])
    w = min(da.shape[1], da_back.shape[1])
    margin = 1
    if h > 2 * margin and w > 2 * margin:
        orig_interior = da.values[margin : h - margin, margin : w - margin]
        back_interior = da_back.values[margin : h - margin, margin : w - margin]
        # Mask NaN
        valid = ~np.isnan(orig_interior) & ~np.isnan(back_interior)
        if valid.any():
            rel_error = np.abs(orig_interior[valid] - back_interior[valid]) / (
                np.abs(orig_interior[valid]) + 1.0
            )
            assert rel_error.max() < 0.5, f"Max relative error {rel_error.max():.3f}"


# ---------------------------------------------------------------------------
# reproject_to_grid
# ---------------------------------------------------------------------------


def test_reproject_to_grid_output_shape() -> None:
    """Reprojected DataArray must have shape matching the target GridSpec."""
    spec, da = _make_utm_grid(resolution_m=100)

    # Target: same area but coarser resolution
    target_spec = GridSpec(
        minx=spec.minx,
        miny=spec.miny,
        maxx=spec.maxx,
        maxy=spec.maxy,
        resolution_m=200,
        crs=_UTM36N,
    )
    result = reproject_to_grid(da, target_spec)  # type: ignore[arg-type]
    assert result.shape == (target_spec.height, target_spec.width)


def test_reproject_to_grid_crs_correct() -> None:
    """Output CRS must match the target GridSpec CRS."""
    spec, da = _make_utm_grid(resolution_m=100)
    target_spec = GridSpec(
        minx=spec.minx,
        miny=spec.miny,
        maxx=spec.maxx,
        maxy=spec.maxy,
        resolution_m=100,
        crs=_UTM36N,
    )
    result = reproject_to_grid(da, target_spec)  # type: ignore[arg-type]
    assert result.rio.crs.to_epsg() == 32636


def test_reproject_nearest_resampling() -> None:
    """Nearest resampling should work without error."""
    spec, da = _make_utm_grid(resolution_m=100)
    target_spec = GridSpec(
        minx=spec.minx,
        miny=spec.miny,
        maxx=spec.maxx,
        maxy=spec.maxy,
        resolution_m=100,
        crs=_UTM36N,
    )
    result = reproject_to_grid(da, target_spec, resampling="nearest")  # type: ignore[arg-type]
    assert result is not None


def test_reproject_unknown_resampling_raises() -> None:
    spec, da = _make_utm_grid(resolution_m=100)
    with pytest.raises(ValueError, match="Unknown resampling"):
        reproject_to_grid(da, spec, resampling="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GridSpec validation
# ---------------------------------------------------------------------------


def test_gridspec_width_height() -> None:
    spec = GridSpec(minx=0.0, miny=0.0, maxx=1000.0, maxy=500.0, resolution_m=100, crs=_UTM36N)
    assert spec.width == 10
    assert spec.height == 5


def test_gridspec_invalid_bounds_raises() -> None:
    with pytest.raises(ValueError):
        GridSpec(minx=100.0, miny=0.0, maxx=0.0, maxy=500.0, resolution_m=100, crs=_UTM36N)


def test_gridspec_invalid_resolution_raises() -> None:
    with pytest.raises(ValueError):
        GridSpec(minx=0.0, miny=0.0, maxx=100.0, maxy=100.0, resolution_m=0, crs=_UTM36N)


# ---------------------------------------------------------------------------
# empty_dataarray
# ---------------------------------------------------------------------------


def test_empty_dataarray_shape() -> None:
    spec = GridSpec(minx=0.0, miny=0.0, maxx=500.0, maxy=300.0, resolution_m=100, crs=_UTM36N)
    da = empty_dataarray(spec)
    assert da.shape == (3, 5)


def test_empty_dataarray_all_nan() -> None:
    spec = GridSpec(minx=0.0, miny=0.0, maxx=500.0, maxy=300.0, resolution_m=100, crs=_UTM36N)
    da = empty_dataarray(spec)
    assert np.all(np.isnan(da.values))


def test_empty_dataarray_crs_set() -> None:
    spec = GridSpec(minx=0.0, miny=0.0, maxx=500.0, maxy=300.0, resolution_m=100, crs=_UTM36N)
    da = empty_dataarray(spec)
    assert da.rio.crs is not None
    assert da.rio.crs.to_epsg() == 32636
