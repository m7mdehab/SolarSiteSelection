"""Tests for analysis/sites.py — synthetic rasters, hand-verifiable.

Test plan
---------
1. Two separated blobs of class-5 cells; assert 2 regions found, areas
   correct, min-area filter drops tiny blob.
2. mean_lsi computation: hand-checked from a synthetic LSI array.
3. Ranking: unambiguous ordering by mean_lsi; PTL-distance tiebreak.
4. top_k truncation; empty-result case.
5. Polygon geometry in working CRS; .area ≈ cell-count area within tolerance.
6. Centroid columns present; CRS propagated.
7. distance_to_ptl path: mean_ptl_dist populated correctly.
"""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pytest
import rioxarray  # noqa: F401  — registers the .rio accessor on xr.DataArray
import xarray as xr

from solarsite.analysis.sites import SITE_COLUMNS, extract_sites

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RES_M = 1000  # 1 km cells → easy area arithmetic


def _make_grid(
    height: int,
    width: int,
    res_m: int = _RES_M,
    epsg: int = 32636,
) -> tuple[dict[str, object], object]:
    """Return (coords_dict, affine_transform) for a synthetic grid.

    Origin placed at (0, height*res_m) so y-coordinates decrease downward
    (north-up convention matching GridSpec / rioxarray).
    """
    from affine import Affine

    west = 0.0
    north = float(height * res_m)
    transform = Affine(res_m, 0.0, west, 0.0, -res_m, north)
    x_coords = np.arange(width) * res_m + res_m / 2.0
    y_coords = north - np.arange(height) * res_m - res_m / 2.0
    return {"x": x_coords, "y": y_coords}, transform


def _class_da(
    arr: np.ndarray,
    epsg: int = 32636,
    res_m: int = _RES_M,
) -> xr.DataArray:
    """Wrap a 2-D int array as a class DataArray with CRS metadata."""
    height, width = arr.shape
    coords, transform = _make_grid(height, width, res_m=res_m, epsg=epsg)
    da = xr.DataArray(arr.astype(np.int32), dims=["y", "x"], coords=coords, name="lsi_class")
    da = da.rio.write_crs(f"EPSG:{epsg}", inplace=True)
    da = da.rio.write_transform(transform, inplace=True)
    return da


def _lsi_da(
    arr: np.ndarray,
    epsg: int = 32636,
    res_m: int = _RES_M,
) -> xr.DataArray:
    """Wrap a 2-D float array as an LSI DataArray with CRS metadata."""
    height, width = arr.shape
    coords, transform = _make_grid(height, width, res_m=res_m, epsg=epsg)
    da = xr.DataArray(arr.astype(np.float64), dims=["y", "x"], coords=coords, name="lsi")
    da = da.rio.write_crs(f"EPSG:{epsg}", inplace=True)
    da = da.rio.write_transform(transform, inplace=True)
    return da


# Cell area in km² for default 1-km resolution
_CELL_AREA_KM2 = (_RES_M * _RES_M) / 1_000_000.0  # = 1.0 km²


# ---------------------------------------------------------------------------
# 1. Two blobs — region detection and min-area filter
# ---------------------------------------------------------------------------


class TestTwoBlobs:
    """Two separated blobs of class-5 cells."""

    # Layout (5 x 10 grid, 1 km cells):
    #  Blob A: rows 0-1, cols 0-1  → 4 cells → 4 km²  (passes 0.5 km² filter)
    #  Blob B: rows 3-4, cols 7-9  → 6 cells → 6 km²  (passes 0.5 km² filter)
    #  Tiny:   row 0,   col 9      → 1 cell  → 1 km²  (only dropped if filter > 1)
    # All other cells = class 3 (not top-class)

    def _build(self) -> tuple[xr.DataArray, xr.DataArray]:
        cls_arr = np.full((5, 10), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # Blob A: 4 cells
        cls_arr[3:5, 7:10] = 5  # Blob B: 6 cells
        lsi_arr = np.full((5, 10), 0.5, dtype=np.float64)
        return _class_da(cls_arr), _lsi_da(lsi_arr)

    def test_two_regions_found(self) -> None:
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        assert len(gdf) == 2, f"Expected 2 sites, got {len(gdf)}"

    def test_areas_correct(self) -> None:
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        areas = sorted(gdf["area_km2"].tolist())
        assert areas == pytest.approx([4.0, 6.0], rel=1e-6)

    def test_min_area_filter_drops_small_blob(self) -> None:
        """With min_area_km2 = 5.0, only Blob B (6 km²) survives."""
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=5.0, top_k=10)
        assert len(gdf) == 1
        assert gdf.iloc[0]["area_km2"] == pytest.approx(6.0, rel=1e-6)

    def test_polygons_valid(self) -> None:
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        for geom in gdf.geometry:
            assert geom.is_valid, f"Invalid geometry: {geom}"
            assert not geom.is_empty

    def test_polygon_area_matches_cell_count(self) -> None:
        """GeoDataFrame .area (in CRS units = m2) should match cell-count area.

        Blob A: 4 cells * (1000 m)^2 = 4 000 000 m2
        Blob B: 6 cells * (1000 m)^2 = 6 000 000 m2
        """
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        for _, row in gdf.iterrows():
            expected_m2 = row["area_km2"] * 1_000_000.0
            # Allow ±0.1 % tolerance for polygon boundary representation
            assert row.geometry.area == pytest.approx(expected_m2, rel=1e-3)

    def test_crs_propagated(self) -> None:
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        assert gdf.crs is not None
        assert gdf.crs.to_epsg() == 32636

    def test_schema_columns(self) -> None:
        cls_da, lsi_da = self._build()
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        for col in SITE_COLUMNS:
            assert col in gdf.columns, f"Missing column: {col}"
        assert "geometry" in gdf.columns

    def test_tiny_blob_single_cell_dropped_by_tight_filter(self) -> None:
        """A 1-cell blob (1 km²) is dropped when min_area_km2 = 1.5."""
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # 4 cells → 4 km²  (passes)
        cls_arr[3, 3] = 5  # 1 cell  → 1 km²  (dropped)
        lsi_arr = np.full((4, 4), 0.5, dtype=np.float64)
        cls_da = _class_da(cls_arr)
        lsi_da = _lsi_da(lsi_arr)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=1.5, top_k=10)
        assert len(gdf) == 1
        assert gdf.iloc[0]["area_km2"] == pytest.approx(4.0, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. mean_lsi computation
# ---------------------------------------------------------------------------


class TestMeanLsi:
    """Verify mean_lsi and max_lsi are computed correctly."""

    def test_mean_lsi_hand_checked(self) -> None:
        """4-cell blob with known LSI values → mean/max hand-verified.

        Blob cells (rows 0-1, cols 0-1):  LSI = 0.2, 0.4, 0.6, 0.8
        Expected mean_lsi = 0.5, max_lsi = 0.8
        """
        cls_arr = np.full((3, 3), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # 4-cell blob
        lsi_arr = np.zeros((3, 3), dtype=np.float64)
        lsi_arr[0, 0] = 0.2
        lsi_arr[0, 1] = 0.4
        lsi_arr[1, 0] = 0.6
        lsi_arr[1, 1] = 0.8

        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        assert gdf.iloc[0]["mean_lsi"] == pytest.approx(0.5, rel=1e-9)
        assert gdf.iloc[0]["max_lsi"] == pytest.approx(0.8, rel=1e-9)

    def test_nan_lsi_cells_skipped(self) -> None:
        """NaN LSI cells are excluded from mean/max computation."""
        cls_arr = np.full((2, 3), 3, dtype=np.int32)
        cls_arr[0, 0:3] = 5  # 3-cell row blob
        lsi_arr = np.array([[0.3, np.nan, 0.9], [0.0, 0.0, 0.0]], dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        # mean over {0.3, 0.9} = 0.6; max = 0.9
        assert gdf.iloc[0]["mean_lsi"] == pytest.approx(0.6, rel=1e-9)
        assert gdf.iloc[0]["max_lsi"] == pytest.approx(0.9, rel=1e-9)

    def test_multiple_top_classes(self) -> None:
        """top_classes=(4, 5) merges cells of class 4 and class 5."""
        cls_arr = np.array(
            [[5, 5, 3, 3], [4, 4, 3, 3], [3, 3, 3, 3]],
            dtype=np.int32,
        )
        lsi_arr = np.ones((3, 4), dtype=np.float64) * 0.7
        gdf = extract_sites(
            _lsi_da(lsi_arr),
            _class_da(cls_arr),
            top_classes=(4, 5),
            min_area_km2=0.5,
            top_k=5,
        )
        # 4-cell region (rows 0-1, cols 0-1) should be found
        assert len(gdf) >= 1
        assert gdf.iloc[0]["mean_lsi"] == pytest.approx(0.7, rel=1e-9)


# ---------------------------------------------------------------------------
# 3. Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    """Verify the ranking composite produces the correct order."""

    def _two_site_setup(
        self,
        lsi_site_a: float = 0.9,
        lsi_site_b: float = 0.6,
        area_a: int = 4,
        area_b: int = 4,
    ) -> tuple[xr.DataArray, xr.DataArray]:
        """Two isolated blobs with known LSI values.

        Grid: 5 x 10.
        Site A: rows 0-1, cols 0-1  (area_a cells, all with lsi_site_a).
        Site B: rows 3-4, cols 7-9  (6 cells, all with lsi_site_b).
        The area_a parameter is unused beyond documentation here; both blobs
        are fixed at 4 and 6 cells respectively.
        """
        cls_arr = np.full((5, 10), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # Site A: 4 cells
        cls_arr[3:5, 7:10] = 5  # Site B: 6 cells
        lsi_arr = np.full((5, 10), 0.0, dtype=np.float64)
        lsi_arr[0:2, 0:2] = lsi_site_a
        lsi_arr[3:5, 7:10] = lsi_site_b
        return _class_da(cls_arr), _lsi_da(lsi_arr)

    def test_higher_mean_lsi_ranks_first(self) -> None:
        """Site A (mean_lsi=0.9) should rank 1 over Site B (mean_lsi=0.6)."""
        cls_da, lsi_da = self._two_site_setup(lsi_site_a=0.9, lsi_site_b=0.6)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        assert gdf.iloc[0]["rank"] == 1
        assert gdf.iloc[0]["mean_lsi"] == pytest.approx(0.9, rel=1e-6)
        assert gdf.iloc[1]["rank"] == 2
        assert gdf.iloc[1]["mean_lsi"] == pytest.approx(0.6, rel=1e-6)

    def test_equal_lsi_larger_area_ranks_first(self) -> None:
        """Tie on mean_lsi → larger area_km2 should rank first.

        Site A: 4 cells, mean_lsi=0.7.
        Site B: 6 cells, mean_lsi=0.7.
        Site B should rank 1 (larger area).
        """
        cls_arr = np.full((5, 10), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # Site A: 4 cells
        cls_arr[3:5, 7:10] = 5  # Site B: 6 cells
        lsi_arr = np.full((5, 10), 0.7, dtype=np.float64)
        cls_da = _class_da(cls_arr)
        lsi_da = _lsi_da(lsi_arr)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=10)
        assert gdf.iloc[0]["area_km2"] == pytest.approx(6.0, rel=1e-6)
        assert gdf.iloc[0]["rank"] == 1

    def test_ptl_distance_tiebreak(self) -> None:
        """Equal mean_lsi and equal area → closer PTL distance ranks first.

        Both blobs: 4 cells, mean_lsi=0.8.
        PTL raster: Site A cells have dist=500 m, Site B cells have dist=2000 m.
        Site A should rank 1 (lower mean_ptl_dist).
        """
        cls_arr = np.full((4, 8), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # Site A: 4 cells
        cls_arr[0:2, 5:7] = 5  # Site B: 4 cells
        lsi_arr = np.full((4, 8), 0.8, dtype=np.float64)
        ptl_arr = np.full((4, 8), 5000.0, dtype=np.float64)
        ptl_arr[0:2, 0:2] = 500.0  # Site A close to PTL
        ptl_arr[0:2, 5:7] = 2000.0  # Site B far from PTL
        cls_da = _class_da(cls_arr)
        lsi_da = _lsi_da(lsi_arr)
        ptl_da = _lsi_da(ptl_arr)  # reuse helper; same shape
        gdf = extract_sites(
            lsi_da,
            cls_da,
            min_area_km2=0.5,
            distance_to_ptl=ptl_da,
            top_k=10,
        )
        assert gdf.iloc[0]["rank"] == 1
        assert gdf.iloc[0]["mean_ptl_dist"] == pytest.approx(500.0, rel=1e-6)
        assert gdf.iloc[1]["mean_ptl_dist"] == pytest.approx(2000.0, rel=1e-6)

    def test_mean_ptl_dist_populated(self) -> None:
        """When distance_to_ptl is provided, mean_ptl_dist column is not NaN."""
        cls_arr = np.full((3, 3), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5
        lsi_arr = np.full((3, 3), 0.5, dtype=np.float64)
        ptl_arr = np.full((3, 3), 1000.0, dtype=np.float64)
        gdf = extract_sites(
            _lsi_da(lsi_arr),
            _class_da(cls_arr),
            min_area_km2=0.5,
            distance_to_ptl=_lsi_da(ptl_arr),
            top_k=5,
        )
        assert len(gdf) == 1
        assert not math.isnan(gdf.iloc[0]["mean_ptl_dist"])
        assert gdf.iloc[0]["mean_ptl_dist"] == pytest.approx(1000.0, rel=1e-6)

    def test_mean_ptl_dist_nan_when_not_provided(self) -> None:
        """Without distance_to_ptl, mean_ptl_dist column is NaN."""
        cls_arr = np.full((3, 3), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5
        lsi_arr = np.full((3, 3), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        assert math.isnan(gdf.iloc[0]["mean_ptl_dist"])

    def test_rank_column_starts_at_one(self) -> None:
        cls_arr = np.full((5, 10), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5
        cls_arr[3:5, 7:10] = 5
        lsi_arr = np.full((5, 10), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=10)
        assert gdf["rank"].min() == 1
        ranks = sorted(gdf["rank"].tolist())
        assert ranks == list(range(1, len(gdf) + 1))


# ---------------------------------------------------------------------------
# 4. top_k truncation and empty-result case
# ---------------------------------------------------------------------------


class TestTopKAndEmpty:
    """top_k truncation and empty GeoDataFrame when no region passes filter."""

    def _many_blobs(self, n: int = 5) -> tuple[xr.DataArray, xr.DataArray]:
        """n blobs of 4 cells each, spaced so they don't touch."""
        h = 3
        w = n * 5
        cls_arr = np.full((h, w), 3, dtype=np.int32)
        lsi_arr = np.zeros((h, w), dtype=np.float64)
        for i in range(n):
            c = i * 5
            cls_arr[0:2, c : c + 2] = 5
            lsi_arr[0:2, c : c + 2] = 0.5 + i * 0.05  # different LSI per blob
        return _class_da(cls_arr), _lsi_da(lsi_arr)

    def test_top_k_limits_results(self) -> None:
        cls_da, lsi_da = self._many_blobs(n=5)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=3)
        assert len(gdf) == 3

    def test_top_k_larger_than_sites_returns_all(self) -> None:
        cls_da, lsi_da = self._many_blobs(n=5)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=100)
        assert len(gdf) == 5

    def test_empty_result_no_crash(self) -> None:
        """All cells are class 3; no top-class cells → empty GDF."""
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        lsi_arr = np.full((4, 4), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=10)
        assert len(gdf) == 0
        assert isinstance(gdf, gpd.GeoDataFrame)
        for col in SITE_COLUMNS:
            assert col in gdf.columns

    def test_empty_result_from_min_area_filter(self) -> None:
        """All blobs below min_area → empty GDF."""
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        cls_arr[0, 0] = 5  # 1-cell blob = 1 km²
        lsi_arr = np.full((4, 4), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=2.0, top_k=10)
        assert len(gdf) == 0

    def test_empty_result_has_correct_crs(self) -> None:
        cls_arr = np.full((3, 3), 3, dtype=np.int32)
        lsi_arr = np.full((3, 3), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=999.0, top_k=10)
        assert gdf.crs is not None
        assert gdf.crs.to_epsg() == 32636


# ---------------------------------------------------------------------------
# 5. Geometry and CRS
# ---------------------------------------------------------------------------


class TestGeometry:
    """Polygon geometry, CRS, and centroid columns."""

    def test_geometry_in_working_crs(self) -> None:
        """Polygon bounds should be within the working-CRS extent."""
        cls_arr = np.full((5, 5), 3, dtype=np.int32)
        cls_arr[1:3, 1:3] = 5  # 4-cell blob
        lsi_arr = np.full((5, 5), 0.6, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        # Grid extents: x in [0, 5000], y in [0, 5000]
        geom = gdf.geometry.iloc[0]
        minx, miny, maxx, maxy = geom.bounds
        assert minx >= 0.0
        assert miny >= 0.0
        assert maxx <= 5 * _RES_M
        assert maxy <= 5 * _RES_M

    def test_centroid_columns_finite(self) -> None:
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5
        lsi_arr = np.full((4, 4), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        row = gdf.iloc[0]
        assert math.isfinite(row["centroid_x"])
        assert math.isfinite(row["centroid_y"])

    def test_centroid_lon_lat_reasonable(self) -> None:
        """WGS-84 centroid should yield lon/lat in plausible ranges."""
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5
        lsi_arr = np.full((4, 4), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        row = gdf.iloc[0]
        # If CRS reprojection succeeded, lon/lat should not be NaN
        # and should be in valid world ranges
        if not math.isnan(row["centroid_lon"]):
            assert -180.0 <= row["centroid_lon"] <= 180.0
            assert -90.0 <= row["centroid_lat"] <= 90.0

    def test_polygon_area_approx_cell_count(self) -> None:
        """gdf.geometry.area (m2) approx area_km2 * 1e6 within 0.1 %."""
        cls_arr = np.full((6, 6), 3, dtype=np.int32)
        cls_arr[1:4, 1:4] = 5  # 9-cell blob -> 9 km2
        lsi_arr = np.full((6, 6), 0.5, dtype=np.float64)
        gdf = extract_sites(_lsi_da(lsi_arr), _class_da(cls_arr), min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        expected_m2 = gdf.iloc[0]["area_km2"] * 1_000_000.0
        actual_m2 = gdf.geometry.iloc[0].area
        assert actual_m2 == pytest.approx(expected_m2, rel=1e-3)


# ---------------------------------------------------------------------------
# 6. Shape mismatch errors
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Shape-mismatch validation."""

    def test_lsi_class_shape_mismatch_raises(self) -> None:
        lsi_da = _lsi_da(np.zeros((3, 3)))
        cls_da = _class_da(np.full((4, 4), 3, dtype=np.int32))
        with pytest.raises(ValueError, match="shape"):
            extract_sites(lsi_da, cls_da)

    def test_ptl_shape_mismatch_raises(self) -> None:
        cls_da = _class_da(np.full((3, 3), 3, dtype=np.int32))
        lsi_da = _lsi_da(np.zeros((3, 3)))
        ptl_da = _lsi_da(np.zeros((4, 4)))
        with pytest.raises(ValueError, match="shape"):
            extract_sites(lsi_da, cls_da, distance_to_ptl=ptl_da)


# ---------------------------------------------------------------------------
# 7. Simplify tolerance (smoke test)
# ---------------------------------------------------------------------------


class TestSimplify:
    def test_simplify_does_not_crash(self) -> None:
        """simplify_tolerance_m > 0 runs without error."""
        cls_arr = np.full((5, 5), 3, dtype=np.int32)
        cls_arr[1:4, 1:4] = 5
        lsi_arr = np.full((5, 5), 0.5, dtype=np.float64)
        gdf = extract_sites(
            _lsi_da(lsi_arr),
            _class_da(cls_arr),
            min_area_km2=0.5,
            simplify_tolerance_m=100.0,
            top_k=5,
        )
        assert len(gdf) == 1
        assert gdf.geometry.iloc[0].is_valid


# ---------------------------------------------------------------------------
# 8. Fallback transform reconstruction (no rioxarray CRS)
# ---------------------------------------------------------------------------


class TestNoCrs:
    """DataArrays without rioxarray CRS metadata exercise fallback code paths."""

    def _plain_da(self, arr: np.ndarray, res_m: int = _RES_M) -> xr.DataArray:
        """DataArray with explicit x/y coords but NO rioxarray CRS."""
        height, width = arr.shape
        x_coords = np.arange(width) * res_m + res_m / 2.0
        north = float(height * res_m)
        y_coords = north - np.arange(height) * res_m - res_m / 2.0
        return xr.DataArray(arr, dims=["y", "x"], coords={"y": y_coords, "x": x_coords})

    def test_extract_sites_no_crs_returns_gdf(self) -> None:
        """Without CRS metadata, extract_sites runs via fallback transform.

        centroid_lon/centroid_lat will be NaN; area should still be correct.
        """
        cls_arr = np.full((4, 4), 3, dtype=np.int32)
        cls_arr[0:2, 0:2] = 5  # 4-cell blob -> 4 km2
        lsi_arr = np.full((4, 4), 0.7, dtype=np.float64)
        cls_da = self._plain_da(cls_arr.astype(np.int32))
        lsi_da = self._plain_da(lsi_arr)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=0.5, top_k=5)
        assert len(gdf) == 1
        assert gdf.iloc[0]["area_km2"] == pytest.approx(4.0, rel=1e-6)
        assert gdf.iloc[0]["mean_lsi"] == pytest.approx(0.7, rel=1e-9)
        # Without CRS, centroid_lon/lat should be NaN (no reprojection possible)
        assert math.isnan(gdf.iloc[0]["centroid_lon"])
        assert math.isnan(gdf.iloc[0]["centroid_lat"])

    def test_empty_result_no_crs(self) -> None:
        """Empty result with no-CRS DataArray returns GeoDataFrame with None CRS."""
        cls_arr = np.full((3, 3), 3, dtype=np.int32)
        lsi_arr = np.full((3, 3), 0.5, dtype=np.float64)
        cls_da = self._plain_da(cls_arr.astype(np.int32))
        lsi_da = self._plain_da(lsi_arr)
        gdf = extract_sites(lsi_da, cls_da, min_area_km2=999.0, top_k=5)
        assert len(gdf) == 0
        # CRS is None (no metadata on input), GeoDataFrame is still valid
        assert isinstance(gdf, gpd.GeoDataFrame)
