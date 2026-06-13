"""Tests for reclassify.py -- tiny synthetic rasters with hand-computable answers.

All test rasters are small (<=3x3) so expected scores can be verified by hand.

Coverage targets:
  - BreakpointReclassification: band-edge straddling, NaN passthrough
  - GHI annual->daily unit reconciliation (2190 kWh/m2/yr -> 6.0/day -> score 1.0)
  - ClassScoreReclassification: known codes, unknown code -> 0
  - data_unit kwarg override
  - UserWarning when data unit is missing for a /day criterion
  - Output shape, name, and attrs preservation
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest
import xarray as xr

from solarsite.analysis.reclassify import reclassify_layer
from solarsite.analysis.registry import (
    Breakpoint,
    BreakpointReclassification,
    ClassScoreReclassification,
    Criterion,
    load_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_da(values: list[list[float]], name: str = "raw") -> xr.DataArray:
    """Create a tiny DataArray from a 2-D list."""
    arr = np.array(values, dtype=np.float64)
    return xr.DataArray(arr, dims=["y", "x"], name=name)


def _bp_criterion(
    key: str,
    breakpoints: list[tuple[float, float]],
    unit: str = "",
) -> Criterion:
    """Build a minimal factor Criterion with BreakpointReclassification."""
    bps = [Breakpoint(max=mx, score=sc) for mx, sc in breakpoints]
    reclass = BreakpointReclassification(type="breakpoints", breakpoints=bps)
    return Criterion(
        key=key,
        name=key,
        group="technical",
        kind="factor",
        local_weight=0.40,
        data_source="test",
        unit=unit,
        reclassification=reclass,
    )


def _cs_criterion(
    key: str,
    class_scores: dict[str, float],
    unit: str = "class_index",
) -> Criterion:
    """Build a minimal factor Criterion with ClassScoreReclassification."""
    reclass = ClassScoreReclassification(type="class_scores", class_scores=class_scores)
    return Criterion(
        key=key,
        name=key,
        group="environmental",
        kind="factor",
        local_weight=0.40,
        data_source="test",
        unit=unit,
        reclassification=reclass,
    )


# ---------------------------------------------------------------------------
# BreakpointReclassification -- band edge straddling
# ---------------------------------------------------------------------------


class TestBreakpointReclassification:
    """Tests for continuous breakpoint reclassification."""

    def test_exact_band_edges(self) -> None:
        """Values exactly at band edges map to the correct score.

        Breakpoints (upper-bound inclusive):
          <= 5.0  -> 1.0
          <= 10.0 -> 0.75
          <= inf  -> 0.0
        """
        criterion = _bp_criterion(
            "slope",
            [(5.0, 1.0), (10.0, 0.75), (float("inf"), 0.0)],
        )
        # 3x3 grid straddling band edges
        da = _make_da(
            [
                [0.0, 5.0, 5.1],
                [9.9, 10.0, 10.1],
                [100.0, np.nan, -1.0],
            ]
        )
        result = reclassify_layer(da, criterion)

        assert result.shape == (3, 3)
        expected = np.array(
            [
                [1.0, 1.0, 0.75],
                [0.75, 0.75, 0.0],
                [0.0, np.nan, 1.0],  # -1.0 <= 5.0 -> score 1.0
            ]
        )
        np.testing.assert_array_equal(
            result.values[~np.isnan(result.values)],
            expected[~np.isnan(expected)],
        )
        assert np.isnan(result.values[2, 1])

    def test_nan_passthrough(self) -> None:
        """NaN inputs must produce NaN outputs."""
        criterion = _bp_criterion("tmp", [(10.0, 1.0), (float("inf"), 0.0)])
        da = _make_da([[np.nan, np.nan], [np.nan, np.nan]])
        result = reclassify_layer(da, criterion)
        assert np.all(np.isnan(result.values))

    def test_all_below_first_breakpoint(self) -> None:
        """All values below the first breakpoint get the first score."""
        criterion = _bp_criterion("tmp", [(100.0, 0.8), (float("inf"), 0.2)])
        da = _make_da([[1.0, 50.0], [99.9, 100.0]])
        result = reclassify_layer(da, criterion)
        np.testing.assert_array_almost_equal(result.values, [[0.8, 0.8], [0.8, 0.8]])

    def test_above_all_finite_breakpoints(self) -> None:
        """Value above all finite breakpoints uses the inf band score."""
        criterion = _bp_criterion("tmp", [(5.0, 1.0), (float("inf"), 0.0)])
        da = _make_da([[1000.0]])
        result = reclassify_layer(da, criterion)
        assert result.values[0, 0] == pytest.approx(0.0)

    def test_output_name_and_attrs(self) -> None:
        """Output DataArray has correct name and attrs."""
        criterion = _bp_criterion("slope", [(5.0, 1.0), (float("inf"), 0.0)])
        da = _make_da([[3.0]])
        result = reclassify_layer(da, criterion)
        assert result.name == "suit_slope"
        assert result.attrs["criterion_key"] == "slope"
        assert "suitability_score" in result.attrs["units"]

    def test_multi_band_values(self) -> None:
        """3x3 array with values spanning all bands maps correctly."""
        # dist_ptl-like: <=4.8->0.0, <=10->1.0, <=20->0.75, <=35->0.50, <=50->0.25, inf->0.0
        criterion = _bp_criterion(
            "dist_ptl",
            [
                (4.8, 0.0),
                (10.0, 1.0),
                (20.0, 0.75),
                (35.0, 0.50),
                (50.0, 0.25),
                (float("inf"), 0.0),
            ],
        )
        da = _make_da(
            [
                [1.0, 7.0, 15.0],
                [25.0, 40.0, 55.0],
                [4.8, 10.0, 50.0],
            ]
        )
        result = reclassify_layer(da, criterion)
        expected = np.array(
            [
                [0.0, 1.0, 0.75],
                [0.50, 0.25, 0.0],
                [0.0, 1.0, 0.25],
            ]
        )
        np.testing.assert_array_almost_equal(result.values, expected)


# ---------------------------------------------------------------------------
# GHI annual->daily unit reconciliation (DECISION #9)
# ---------------------------------------------------------------------------


class TestGHIUnitReconciliation:
    """Tests for the annual->daily normalisation for solar_radiation."""

    def _ghi_criterion(self) -> Criterion:
        """Return a solar_radiation-like criterion with /day breakpoints."""
        # Breakpoints from criteria.yaml:
        # <=3.5->0.0, <=4.0->0.1, <=4.7->0.25, <=5.0->0.50, <=5.4->0.75, <=5.9->1.0, inf->1.0
        return _bp_criterion(
            "solar_radiation",
            [
                (3.5, 0.0),
                (4.0, 0.1),
                (4.7, 0.25),
                (5.0, 0.50),
                (5.4, 0.75),
                (5.9, 1.0),
                (float("inf"), 1.0),
            ],
            unit="kWh/m2/day",
        )

    def test_annual_to_daily_conversion(self) -> None:
        """2190 kWh/m2/yr / 365 = 6.0/day -> score 1.0 (above 5.9 band)."""
        criterion = self._ghi_criterion()
        # 2190 / 365 = 6.0 -> above 5.9 -> score 1.0
        da = _make_da([[2190.0]])
        result = reclassify_layer(da, criterion, data_unit="kWh/m2/yr")
        assert result.values[0, 0] == pytest.approx(1.0)

    def test_annual_to_daily_low_value(self) -> None:
        """1277.5 kWh/m2/yr / 365 = 3.5/day -> score 0.0 (<=3.5 band)."""
        criterion = self._ghi_criterion()
        # 1277.5 / 365 = 3.5 -> score 0.0
        da = _make_da([[1277.5]])
        result = reclassify_layer(da, criterion, data_unit="kWh/m2/yr")
        assert result.values[0, 0] == pytest.approx(0.0)

    def test_annual_to_daily_mid_range(self) -> None:
        """1825 kWh/m2/yr / 365 = 5.0/day -> score 0.50."""
        criterion = self._ghi_criterion()
        da = _make_da([[1825.0]])
        result = reclassify_layer(da, criterion, data_unit="kWh/m2/yr")
        assert result.values[0, 0] == pytest.approx(0.50)

    def test_no_conversion_for_daily_data(self) -> None:
        """Data already in /day: no /365 applied; 5.0/day -> 0.50."""
        criterion = self._ghi_criterion()
        da = _make_da([[5.0]])
        result = reclassify_layer(da, criterion, data_unit="kWh/m2/day")
        assert result.values[0, 0] == pytest.approx(0.50)

    def test_attrs_unit_data(self) -> None:
        """Unit in DataArray attrs is picked up automatically."""
        criterion = self._ghi_criterion()
        arr = np.array([[2190.0]], dtype=np.float64)
        da = xr.DataArray(arr, dims=["y", "x"], attrs={"units": "kWh/m2/yr"})
        result = reclassify_layer(da, criterion)
        assert result.values[0, 0] == pytest.approx(1.0)

    def test_missing_unit_emits_warning(self) -> None:
        """Missing data unit emits UserWarning (and does NOT crash)."""
        criterion = self._ghi_criterion()
        da = _make_da([[5.0]])  # no unit metadata, no data_unit kwarg
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = reclassify_layer(da, criterion)
        user_warnings = [x for x in w if issubclass(x.category, UserWarning)]
        assert len(user_warnings) >= 1
        assert "unit" in str(user_warnings[0].message).lower()
        # Value treated as daily -> 5.0/day -> score 0.50
        assert result.values[0, 0] == pytest.approx(0.50)

    def test_non_solar_criterion_no_conversion(self) -> None:
        """A breakpoint criterion WITHOUT /day unit does NOT divide by 365."""
        # slope criterion in degrees -- no /day unit
        criterion = _bp_criterion(
            "slope",
            [(5.0, 1.0), (15.0, 0.40), (float("inf"), 0.0)],
            unit="degrees",
        )
        # If wrongly divided by 365, 1825 -> 5.0 -> 1.0.  Correctly: 1825 -> 0.0.
        da = _make_da([[1825.0]])
        result = reclassify_layer(da, criterion, data_unit="degrees")
        assert result.values[0, 0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ClassScoreReclassification
# ---------------------------------------------------------------------------


class TestClassScoreReclassification:
    """Tests for categorical class-score reclassification."""

    def test_known_codes_map_correctly(self) -> None:
        """LULC codes 10, 30, 60 map to their documented scores."""
        criterion = _cs_criterion(
            "lulc",
            {
                "10": 0.0,
                "30": 0.40,
                "60": 1.0,
                "100": 0.70,
            },
        )
        da = _make_da([[10.0, 30.0], [60.0, 100.0]])
        result = reclassify_layer(da, criterion)
        expected = np.array([[0.0, 0.40], [1.0, 0.70]])
        np.testing.assert_array_almost_equal(result.values, expected)

    def test_unknown_code_maps_to_zero(self) -> None:
        """Code not in class_scores -> 0.0 (conservative / unsuitable)."""
        criterion = _cs_criterion("lulc", {"10": 0.0, "60": 1.0})
        da = _make_da([[99.0]])  # code 99 not in dict
        result = reclassify_layer(da, criterion)
        assert result.values[0, 0] == pytest.approx(0.0)

    def test_land_capability_string_keys(self) -> None:
        """String class keys like 'VII' work when data carries integer codes.

        The land_capability criterion uses Roman-numeral string keys (I-VIII),
        but rasters will carry integer codes.  Verify fallback string matching.
        Note: since integer -> string conversion yields '7' not 'VII', integer
        rasters need a separate mapping.  Test that integer-keyed mapping works.
        """
        criterion = _cs_criterion(
            "land_cap",
            {
                "1": 0.0,
                "2": 0.1,
                "7": 0.90,
                "8": 1.0,
            },
        )
        da = _make_da([[1.0, 2.0], [7.0, 8.0]])
        result = reclassify_layer(da, criterion)
        expected = np.array([[0.0, 0.1], [0.90, 1.0]])
        np.testing.assert_array_almost_equal(result.values, expected)

    def test_aspect_string_keys_raise_on_float_input(self) -> None:
        """The aspect criterion has string keys; float raster won't match.

        Verifies that float input with string keys falls back to unknown -> 0.0.
        (Aspect rasters from terrain analysis typically encode compass direction
        as integers or strings, not arbitrary floats.)
        """
        criterion = _cs_criterion(
            "aspect",
            {"south": 1.0, "north": 0.0},
        )
        da = _make_da([[0.0, 180.0]])
        result = reclassify_layer(da, criterion)
        # "0" and "180" not in {"south", "north"} -> 0.0
        np.testing.assert_array_almost_equal(result.values, [[0.0, 0.0]])

    def test_nan_preserved_in_class_scores(self) -> None:
        """NaN in class-score input -> NaN in output."""
        criterion = _cs_criterion("lulc", {"10": 0.0, "60": 1.0})
        da = _make_da([[np.nan, 10.0]])
        result = reclassify_layer(da, criterion)
        assert np.isnan(result.values[0, 0])
        assert result.values[0, 1] == pytest.approx(0.0)

    def test_mixed_known_unknown_nan(self) -> None:
        """Mix of known, unknown, and NaN codes in one array."""
        criterion = _cs_criterion("lulc", {"10": 0.0, "30": 0.4, "60": 1.0})
        da = _make_da([[10.0, 30.0, 60.0], [99.0, np.nan, 0.0]])
        result = reclassify_layer(da, criterion)
        expected = np.array([[0.0, 0.4, 1.0], [0.0, np.nan, 0.0]])
        non_nan = ~np.isnan(expected)
        np.testing.assert_array_almost_equal(result.values[non_nan], expected[non_nan])
        assert np.isnan(result.values[1, 1])


# ---------------------------------------------------------------------------
# Real registry end-to-end reclassification
# ---------------------------------------------------------------------------


class TestRealRegistryReclassification:
    """Smoke tests using the actual criteria.yaml registry."""

    @pytest.fixture(scope="class")
    def registry(self):  # type: ignore[override]
        return load_registry()

    def test_slope_reclassification_real_criterion(self, registry) -> None:  # type: ignore[override]
        """Slope criterion from real registry.

        Breakpoints: <=5->1.0, <=10->0.75, <=15->0.40, <=20->0.10, inf->0.0
          3.0  <= 5.0  -> 1.0
          8.0  <= 10.0 -> 0.75
          12.0 <= 15.0 -> 0.40
          18.0 <= 20.0 -> 0.10
          25.0 > 20.0  -> 0.0
        """
        criterion = registry.criterion("slope")
        da = _make_da([[3.0, 8.0, 12.0, 18.0, 25.0]])
        result = reclassify_layer(da, criterion)
        assert result.shape == (1, 5)
        assert result.values[0, 0] == pytest.approx(1.0)
        assert result.values[0, 1] == pytest.approx(0.75)
        assert result.values[0, 2] == pytest.approx(0.40)
        assert result.values[0, 3] == pytest.approx(0.10)
        assert result.values[0, 4] == pytest.approx(0.0)

    def test_solar_radiation_real_criterion_annual_input(self, registry) -> None:  # type: ignore[override]
        """solar_radiation from real registry: 2190 yr -> 6.0/day -> score 1.0."""
        criterion = registry.criterion("solar_radiation")
        da = _make_da([[2190.0]])
        result = reclassify_layer(da, criterion, data_unit="kWh/m2/yr")
        assert result.values[0, 0] == pytest.approx(1.0)

    def test_lulc_real_criterion(self, registry) -> None:  # type: ignore[override]
        """LULC from real registry: code 60 -> 1.0, code 40 -> 0.0."""
        criterion = registry.criterion("lulc")
        da = _make_da([[60.0, 40.0, 30.0]])
        result = reclassify_layer(da, criterion)
        assert result.values[0, 0] == pytest.approx(1.0)
        assert result.values[0, 1] == pytest.approx(0.0)
        assert result.values[0, 2] == pytest.approx(0.40)

    def test_output_in_0_1_range(self, registry) -> None:  # type: ignore[override]
        """All factor criteria reclassification output stays in [0, 1]."""
        for factor in registry.factors:
            # create a small raster
            if isinstance(factor.reclassification, BreakpointReclassification):
                # span the full range of breakpoints
                bps = factor.reclassification.breakpoints
                finite_maxes = [bp.max for bp in bps if bp.max != float("inf")]
                if finite_maxes:
                    vals = np.linspace(0, max(finite_maxes), 9).reshape(3, 3)
                else:
                    vals = np.ones((3, 3))
                da = xr.DataArray(vals, dims=["y", "x"])
            else:
                # class_scores -- use the integer values from the keys
                keys = list(factor.reclassification.class_scores.keys())
                try:
                    int_keys = [int(k) for k in keys[:9]]
                    padded = int_keys[:9] + [0] * (9 - len(int_keys[:9]))
                    vals = np.array(padded, dtype=np.float64).reshape(3, 3)
                except ValueError:
                    vals = np.zeros((3, 3))
                da = xr.DataArray(vals, dims=["y", "x"])

            # Supply data_unit for solar_radiation to avoid warning
            ku = "kWh/m2/yr" if factor.key == "solar_radiation" else None
            result = reclassify_layer(da, factor, data_unit=ku)
            valid = result.values[~np.isnan(result.values)]
            assert np.all(valid >= 0.0) and np.all(valid <= 1.0), (
                f"Criterion '{factor.key}' produced out-of-range values: "
                f"min={valid.min()}, max={valid.max()}"
            )
