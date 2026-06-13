"""Tests for overlay.py -- tiny synthetic rasters with hand-computable answers.

Test coverage:
  - build_exclusion_mask: single mask, multiple masks OR'd, unknown-key warning
  - weighted_overlay: 2-3 layers with known weights, renorm for missing layers
  - apply_exclusions: mask zeroes / NaN-fills correct cells
  - classify_lsi: quantile + equal_interval, 5-class assignment
  - End-to-end integration over all real registry factor criteria with synthetic
    suitability layers: LSI in [0,1], shape preserved, no unexpected NaNs
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
import xarray as xr

from solarsite.analysis.overlay import (
    apply_exclusions,
    build_exclusion_mask,
    classify_lsi,
    weighted_overlay,
)
from solarsite.analysis.registry import load_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _da(values: list[list[float]], name: str = "layer") -> xr.DataArray:
    """Create a tiny (y,x) DataArray from a 2-D list."""
    arr = np.array(values, dtype=np.float64)
    return xr.DataArray(arr, dims=["y", "x"], name=name)


def _bool_da(values: list[list[bool]], name: str = "mask") -> xr.DataArray:
    arr = np.array(values, dtype=bool)
    return xr.DataArray(arr, dims=["y", "x"], name=name)


# ---------------------------------------------------------------------------
# build_exclusion_mask
# ---------------------------------------------------------------------------


class TestBuildExclusionMask:
    """Tests for the exclusion mask builder."""

    @pytest.fixture
    def registry(self):  # type: ignore[override]
        return load_registry()

    def test_single_mask_passthrough(self, registry) -> None:  # type: ignore[override]
        """A single binary mask is returned unchanged (modulo dtype cast)."""
        mask = _bool_da([[True, False], [False, True]], name="excl_wdpa")
        result = build_exclusion_mask({"excl_wdpa": mask}, registry)
        assert result.shape == (2, 2)
        np.testing.assert_array_equal(result.values, [[True, False], [False, True]])

    def test_two_masks_logical_or(self, registry) -> None:  # type: ignore[override]
        """Two masks combined with OR -- hand-computable result.

        mask1:  [[True,  False], [False, False]]
        mask2:  [[False, False], [False, True ]]
        OR:     [[True,  False], [False, True ]]
        """
        m1 = _bool_da([[True, False], [False, False]], name="excl_wdpa")
        m2 = _bool_da([[False, False], [False, True]], name="excl_water")
        result = build_exclusion_mask({"excl_wdpa": m1, "excl_water": m2}, registry)
        expected = np.array([[True, False], [False, True]])
        np.testing.assert_array_equal(result.values, expected)

    def test_numeric_mask_nonzero_is_excluded(self, registry) -> None:  # type: ignore[override]
        """Numeric masks: any non-zero value counts as excluded."""
        m = _da([[0.0, 1.0], [0.5, 0.0]], name="excl_slope")
        result = build_exclusion_mask({"excl_slope": m}, registry)
        expected = np.array([[False, True], [True, False]])
        np.testing.assert_array_equal(result.values, expected)

    def test_empty_named_masks_raises(self, registry) -> None:  # type: ignore[override]
        """Empty dict raises ValueError."""
        with pytest.raises(ValueError, match="at least one"):
            build_exclusion_mask({}, registry)

    def test_unknown_key_warns(self, registry) -> None:  # type: ignore[override]
        """Key not in registry hard_exclusions emits UserWarning but still works."""
        m = _bool_da([[True]], name="unknown_excl")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = build_exclusion_mask({"unknown_excl": m}, registry)
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert any("unknown_excl" in str(ww.message) for ww in user_warnings)
        assert result.values[0, 0]

    def test_three_masks_combined(self, registry) -> None:  # type: ignore[override]
        """Three masks: any excluded cell should be True in result."""
        m1 = _bool_da([[False, True]], name="excl_wdpa")
        m2 = _bool_da([[False, False]], name="excl_water")
        m3 = _bool_da([[True, False]], name="excl_urban_core")
        result = build_exclusion_mask(
            {"excl_wdpa": m1, "excl_water": m2, "excl_urban_core": m3}, registry
        )
        np.testing.assert_array_equal(result.values, [[True, True]])

    def test_result_name_and_attrs(self, registry) -> None:  # type: ignore[override]
        """Result has correct name attribute."""
        m = _bool_da([[False]], name="excl_wdpa")
        result = build_exclusion_mask({"excl_wdpa": m}, registry)
        assert result.name == "exclusion_mask"


# ---------------------------------------------------------------------------
# weighted_overlay
# ---------------------------------------------------------------------------


class TestWeightedOverlay:
    """Tests for weighted_overlay -- hand-computed LSI values."""

    @pytest.fixture
    def registry(self):  # type: ignore[override]
        return load_registry()

    def test_two_criteria_hand_computed(self, registry) -> None:  # type: ignore[override]
        """Two criteria: slope + aspect with renormalised weights.

        slope  global_weight = 0.25 * 0.25 = 0.0625
        aspect global_weight = 0.25 * 0.20 = 0.0500
        renorm total = 0.0625 + 0.0500 = 0.1125

        suit_slope  = [[1.0, 1.0], [0.5, 0.5]]
        suit_aspect = [[1.0, 0.0], [1.0, 0.0]]

        renorm_w_slope  = 0.0625 / 0.1125 approx 0.5556
        renorm_w_aspect = 0.0500 / 0.1125 approx 0.4444

        LSI[0,0] = 0.5556*1.0 + 0.4444*1.0 = 1.0
        LSI[0,1] = 0.5556*1.0 + 0.4444*0.0 approx 0.5556
        LSI[1,0] = 0.5556*0.5 + 0.4444*1.0 approx 0.7222
        LSI[1,1] = 0.5556*0.5 + 0.4444*0.0 approx 0.2778
        """
        w_slope = registry.global_weight("slope")
        w_aspect = registry.global_weight("aspect")
        total = w_slope + w_aspect
        rw_slope = w_slope / total
        rw_aspect = w_aspect / total

        suit_slope = _da([[1.0, 1.0], [0.5, 0.5]], name="suit_slope")
        suit_aspect = _da([[1.0, 0.0], [1.0, 0.0]], name="suit_aspect")

        result = weighted_overlay(
            {"slope": suit_slope, "aspect": suit_aspect},
            registry,
        )

        expected = np.array(
            [
                [
                    rw_slope * 1.0 + rw_aspect * 1.0,
                    rw_slope * 1.0 + rw_aspect * 0.0,
                ],
                [
                    rw_slope * 0.5 + rw_aspect * 1.0,
                    rw_slope * 0.5 + rw_aspect * 0.0,
                ],
            ]
        )
        np.testing.assert_array_almost_equal(result.values, expected, decimal=6)

    def test_single_criterion_full_weight(self, registry) -> None:  # type: ignore[override]
        """Single criterion gets renorm weight 1.0 -> LSI == suitability."""
        suit_slope = _da([[0.3, 0.7], [0.9, 0.1]], name="suit_slope")
        result = weighted_overlay({"slope": suit_slope}, registry)
        np.testing.assert_array_almost_equal(result.values, suit_slope.values)

    def test_missing_layer_renormalisation(self, registry) -> None:  # type: ignore[override]
        """With only two layers the weights are renorm'd to sum to 1.0.

        Use slope + shadow (both technical criteria).
        Result LSI must equal sum(renorm_w_i * s_i) for present criteria.
        """
        w_slope = registry.global_weight("slope")
        w_shadow = registry.global_weight("shadow")
        total = w_slope + w_shadow
        rw_slope = w_slope / total
        rw_shadow = w_shadow / total

        suit_slope = _da([[0.8]])
        suit_shadow = _da([[0.4]])

        result = weighted_overlay(
            {"slope": suit_slope, "shadow": suit_shadow},
            registry,
        )
        expected = rw_slope * 0.8 + rw_shadow * 0.4
        assert result.values[0, 0] == pytest.approx(expected, rel=1e-6)

    def test_lsi_in_0_1_range(self, registry) -> None:  # type: ignore[override]
        """With all-1.0 suitability layers, LSI should be 1.0."""
        layers = {f.key: _da([[1.0, 1.0], [1.0, 1.0]]) for f in registry.factors}
        result = weighted_overlay(layers, registry)
        np.testing.assert_array_almost_equal(result.values, [[1.0, 1.0], [1.0, 1.0]])

    def test_all_zero_suitability(self, registry) -> None:  # type: ignore[override]
        """All-zero suitability -> LSI == 0.0."""
        layers = {f.key: _da([[0.0]]) for f in registry.factors}
        result = weighted_overlay(layers, registry)
        assert result.values[0, 0] == pytest.approx(0.0)

    def test_extra_key_warned_and_ignored(self, registry) -> None:  # type: ignore[override]
        """Keys not in registry factors are warned and ignored."""
        suit_slope = _da([[0.5]])
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = weighted_overlay(
                {"slope": suit_slope, "FAKE_KEY": _da([[0.9]])},
                registry,
            )
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert any("FAKE_KEY" in str(ww.message) for ww in user_warnings)
        # Result should equal single-criterion overlay on slope
        single_result = weighted_overlay({"slope": suit_slope}, registry)
        np.testing.assert_array_almost_equal(result.values, single_result.values)

    def test_no_valid_criteria_raises(self, registry) -> None:  # type: ignore[override]
        """No matching criteria keys raises ValueError."""
        with pytest.raises(ValueError, match="no valid factor"):
            weighted_overlay({"FAKE1": _da([[0.5]]), "FAKE2": _da([[0.3]])}, registry)

    def test_nan_in_single_layer_propagates(self, registry) -> None:  # type: ignore[override]
        """NaN in a layer cell: that cell's weight is skipped, others remain."""
        suit_slope = _da([[np.nan, 1.0]])
        suit_shadow = _da([[0.5, 0.5]])
        result = weighted_overlay({"slope": suit_slope, "shadow": suit_shadow}, registry)
        # Cell [0,0]: slope is NaN -> only shadow contributes -> renorm to 1.0
        assert result.values[0, 0] == pytest.approx(0.5, rel=1e-5)
        # Cell [0,1]: both present -> hand-compute renormalised sum
        w_slope = registry.global_weight("slope")
        w_shadow = registry.global_weight("shadow")
        total = w_slope + w_shadow
        rw_slope = w_slope / total
        rw_shadow = w_shadow / total
        expected_01 = rw_slope * 1.0 + rw_shadow * 0.5
        assert result.values[0, 1] == pytest.approx(expected_01, rel=1e-5)

    def test_all_nan_returns_nan(self, registry) -> None:  # type: ignore[override]
        """All-NaN suitability layers produce NaN LSI."""
        layers = {f.key: _da([[np.nan]]) for f in registry.factors}
        result = weighted_overlay(layers, registry)
        assert np.isnan(result.values[0, 0])

    def test_result_attrs(self, registry) -> None:  # type: ignore[override]
        """Result DataArray has 'lsi' name and expected attrs."""
        suit = _da([[0.5]], name="suit_slope")
        result = weighted_overlay({"slope": suit}, registry)
        assert result.name == "lsi"
        assert "criteria_used" in result.attrs


# ---------------------------------------------------------------------------
# apply_exclusions
# ---------------------------------------------------------------------------


class TestApplyExclusions:
    """Tests for apply_exclusions."""

    def test_excluded_cells_become_nan(self) -> None:
        """Masked cells -> NaN (default fill_value)."""
        lsi = _da([[0.8, 0.6], [0.4, 0.2]])
        mask = _bool_da([[True, False], [False, True]])
        result = apply_exclusions(lsi, mask)
        assert np.isnan(result.values[0, 0])
        assert result.values[0, 1] == pytest.approx(0.6)
        assert result.values[1, 0] == pytest.approx(0.4)
        assert np.isnan(result.values[1, 1])

    def test_excluded_cells_become_zero(self) -> None:
        """Masked cells -> 0.0 when fill_value=0.0."""
        lsi = _da([[0.8, 0.6]])
        mask = _bool_da([[True, False]])
        result = apply_exclusions(lsi, mask, fill_value=0.0)
        assert result.values[0, 0] == pytest.approx(0.0)
        assert result.values[0, 1] == pytest.approx(0.6)

    def test_no_exclusions(self) -> None:
        """All-False mask -> result identical to input."""
        lsi = _da([[0.3, 0.7], [0.1, 0.9]])
        mask = _bool_da([[False, False], [False, False]])
        result = apply_exclusions(lsi, mask)
        np.testing.assert_array_almost_equal(result.values, lsi.values)

    def test_all_excluded(self) -> None:
        """All-True mask -> all cells NaN (or fill)."""
        lsi = _da([[0.5, 0.8]])
        mask = _bool_da([[True, True]])
        result = apply_exclusions(lsi, mask)
        assert np.all(np.isnan(result.values))

    def test_numeric_mask_nonzero_excluded(self) -> None:
        """Numeric mask: 0.0 = not excluded, non-zero = excluded."""
        lsi = _da([[0.9, 0.4]])
        mask = _da([[0.0, 5.0]])
        result = apply_exclusions(lsi, mask)
        assert result.values[0, 0] == pytest.approx(0.9)
        assert np.isnan(result.values[0, 1])

    def test_attrs_updated(self) -> None:
        """Result has exclusions_applied in attrs."""
        lsi = _da([[0.5]])
        mask = _bool_da([[False]])
        result = apply_exclusions(lsi, mask)
        assert result.attrs.get("exclusions_applied") is True


# ---------------------------------------------------------------------------
# classify_lsi
# ---------------------------------------------------------------------------


class TestClassifyLsi:
    """Tests for classify_lsi -- quantile and equal_interval methods."""

    def test_quantile_five_classes_known_gradient(self) -> None:
        """A perfect gradient 0.0-1.0 over 5 values -> classes span [1, 5].

        LSI values: [0.0, 0.25, 0.5, 0.75, 1.0]
        With n_classes=5 and quantile method the classes should be
        strictly non-decreasing and span the full [1, 5] range.
        """
        lsi_vals = np.array([[0.0, 0.25, 0.5, 0.75, 1.0]], dtype=np.float64)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi, n_classes=5, method="quantile")
        classes = result.values[0]
        # Should be strictly non-decreasing
        for i in range(len(classes) - 1):
            assert classes[i] <= classes[i + 1], f"Classes not non-decreasing: {classes}"
        # min class = 1, max class = 5
        assert classes.min() == 1
        assert classes.max() == 5

    def test_equal_interval_five_classes(self) -> None:
        """Equal-interval over [0, 1] with 5 classes.

        Values at midpoints of each fifth: 0.1, 0.3, 0.5, 0.7, 0.9
        Expected class assignment: 1, 2, 3, 4, 5
        (boundaries at 0.2, 0.4, 0.6, 0.8 for 0.0-1.0 range)
        """
        lsi_vals = np.array([[0.1, 0.3, 0.5, 0.7, 0.9]], dtype=np.float64)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi, n_classes=5, method="equal_interval")
        classes = result.values[0]
        # Classes should span 1 to 5 and be strictly non-decreasing
        assert classes[0] == 1  # 0.1 is the minimum -> class 1
        assert classes[4] == 5  # 0.9 is the maximum -> class 5
        for i in range(4):
            assert classes[i] <= classes[i + 1]

    def test_nan_preserved_as_nodata(self) -> None:
        """NaN inputs produce NODATA (-9999) in output."""
        lsi_vals = np.array([[np.nan, 0.5, np.nan]], dtype=np.float64)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi)
        assert result.values[0, 0] == -9999
        assert result.values[0, 2] == -9999
        assert result.values[0, 1] != -9999

    def test_all_nan_returns_nodata_grid(self) -> None:
        """All-NaN LSI -> all -9999 output."""
        lsi_vals = np.full((3, 3), np.nan)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi)
        assert np.all(result.values == -9999)

    def test_class_range_1_to_n(self) -> None:
        """Output classes always within [1, n_classes]; no out-of-range values."""
        rng = np.random.default_rng(42)
        lsi_vals = rng.uniform(0, 1, (5, 5))
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        for method in ("quantile", "equal_interval"):
            result = classify_lsi(lsi, n_classes=5, method=method)
            valid = result.values[result.values != -9999]
            assert valid.min() >= 1
            assert valid.max() <= 5

    def test_unknown_method_raises(self) -> None:
        """Unknown method raises ValueError."""
        lsi_vals = np.array([[0.5]], dtype=np.float64)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        with pytest.raises(ValueError, match="Unknown method"):
            classify_lsi(lsi, method="bogus")  # type: ignore[arg-type]

    def test_quantile_uniform_gradient_3x3(self) -> None:
        """3x3 uniform gradient -> classes span [1, 5] with quantile method."""
        lsi_vals = np.linspace(0.0, 1.0, 9).reshape(3, 3)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi, n_classes=5, method="quantile")
        flat = result.values.ravel()
        flat_valid = flat[flat != -9999]
        assert flat_valid.min() >= 1
        assert flat_valid.max() <= 5
        # At least 2 distinct class values should be present
        assert len(np.unique(flat_valid)) >= 2

    def test_equal_interval_constant_input(self) -> None:
        """Constant LSI -> all in the same class (range collapses to a point)."""
        lsi_vals = np.full((2, 2), 0.5)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi, n_classes=5, method="equal_interval")
        # All values same -> thresholds all == 0.5 -> all same class
        flat = result.values.ravel()
        assert np.all(flat == flat[0])

    def test_output_dtype_int32(self) -> None:
        """Output must be integer dtype."""
        lsi_vals = np.array([[0.2, 0.8]], dtype=np.float64)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi)
        assert result.dtype == np.int32

    def test_attrs_contain_method_and_thresholds(self) -> None:
        """Result attrs contain 'method', 'n_classes', and 'thresholds'."""
        lsi_vals = np.linspace(0, 1, 9).reshape(3, 3)
        lsi = xr.DataArray(lsi_vals, dims=["y", "x"])
        result = classify_lsi(lsi, n_classes=5, method="quantile")
        assert result.attrs["method"] == "quantile"
        assert result.attrs["n_classes"] == 5
        assert "thresholds" in result.attrs
        assert len(result.attrs["thresholds"]) == 6  # n_classes + 1


# ---------------------------------------------------------------------------
# End-to-end integration test
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """Full pipeline: weighted_overlay -> exclusion -> classify."""

    @pytest.fixture(scope="class")
    def registry(self):  # type: ignore[override]
        return load_registry()

    def _synthetic_suitability(self, key: str) -> xr.DataArray:
        """Return a small synthetic suitability layer in [0,1]."""
        rng = np.random.default_rng(hash(key) % 2**31)
        vals = rng.uniform(0.0, 1.0, (4, 4)).astype(np.float64)
        return xr.DataArray(vals, dims=["y", "x"], name=f"suit_{key}")

    def test_full_pipeline_all_factors(self, registry) -> None:  # type: ignore[override]
        """End-to-end: synthetic suitability for ALL factors -> LSI in [0,1]."""
        suit_layers = {f.key: self._synthetic_suitability(f.key) for f in registry.factors}

        lsi = weighted_overlay(suit_layers, registry)

        # Shape preserved
        assert lsi.shape == (4, 4)
        # LSI in [0, 1] for all non-NaN cells
        valid = lsi.values[~np.isnan(lsi.values)]
        assert valid.size == 16  # no unexpected NaNs (all inputs are finite)
        assert float(valid.min()) >= 0.0 - 1e-9
        assert float(valid.max()) <= 1.0 + 1e-9

    def test_pipeline_with_exclusions(self, registry) -> None:  # type: ignore[override]
        """Apply a 2-cell exclusion mask and check those cells are NaN."""
        suit_layers = {f.key: self._synthetic_suitability(f.key) for f in registry.factors}
        lsi = weighted_overlay(suit_layers, registry)

        # Mask top-left 2x2
        mask_vals = np.zeros((4, 4), dtype=bool)
        mask_vals[:2, :2] = True
        mask = xr.DataArray(mask_vals, dims=["y", "x"])

        lsi_masked = apply_exclusions(lsi, mask)
        assert np.all(np.isnan(lsi_masked.values[:2, :2]))
        # Bottom-right 2x2 should be unchanged
        np.testing.assert_array_almost_equal(lsi_masked.values[2:, 2:], lsi.values[2:, 2:])

    def test_pipeline_classify_produces_valid_classes(self, registry) -> None:  # type: ignore[override]
        """After classify_lsi, all valid cells are in [1, 5]."""
        suit_layers = {f.key: self._synthetic_suitability(f.key) for f in registry.factors}
        lsi = weighted_overlay(suit_layers, registry)
        classified = classify_lsi(lsi, n_classes=5, method="quantile")

        valid = classified.values[classified.values != -9999]
        assert np.all(valid >= 1)
        assert np.all(valid <= 5)

    def test_pipeline_subset_of_factors(self, registry) -> None:  # type: ignore[override]
        """Pipeline with only 3 factor criteria still produces valid LSI."""
        keys = [f.key for f in registry.factors][:3]
        suit_layers = {k: self._synthetic_suitability(k) for k in keys}

        lsi = weighted_overlay(suit_layers, registry)
        valid = lsi.values[~np.isnan(lsi.values)]
        assert float(valid.min()) >= 0.0 - 1e-9
        assert float(valid.max()) <= 1.0 + 1e-9
