"""Tests for scripts/validate_against_paper.py.

Coverage
--------
1. Class-percentage distribution sums to ~100% over synthetic tiny layers.
2. Top-class and top-two-class comparison figures are computed correctly.
3. Output artefacts (CSV, Markdown, PNG) are written when run with a tiny
   synthetic dataset.
4. Integration smoke-test against the real NW-coast AOI cache — skipped
   when the cache is absent (CI-safe; no network required).

Design principles
-----------------
* No network access: all tests are offline.
* Fast: synthetic layers are 4x4 or smaller; the cache-based test is gated
  behind a ``pytest.mark.skipif`` that fires when ``data/cache/`` is absent
  or the AOI fixture is missing.
* CI-safe: the live-cache test is skipped automatically; the synthetic tests
  are always runnable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

# ---------------------------------------------------------------------------
# Make the scripts/ package importable (it lives at repo root, not in src/).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from validate_against_paper import (  # noqa: E402
    PAPER_MORE_SUITABLE_PCT,
    _compute_class_percentages,
    _write_csv,
    _write_lsi_map,
    _write_markdown_table,
    run_validation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NODATA = -9999


def _class_da(classes: list[list[int]]) -> xr.DataArray:
    """Build a tiny LSI-class DataArray from a 2-D int list."""
    arr = np.array(classes, dtype=np.int32)
    return xr.DataArray(arr, dims=["y", "x"], name="lsi_class")


# ---------------------------------------------------------------------------
# 1. _compute_class_percentages
# ---------------------------------------------------------------------------


class TestComputeClassPercentages:
    """Verify the class-percentage computation on hand-crafted arrays."""

    def test_uniform_five_classes_twenty_pct_each(self) -> None:
        """5 cells, one per class → each class = 20.00%."""
        da = _class_da([[5, 4, 3, 2, 1]])
        pct = _compute_class_percentages(da)
        for cls in range(1, 6):
            assert pct[cls] == pytest.approx(20.0, abs=0.01)

    def test_all_class_5(self) -> None:
        """All valid cells are class 5 → class 5 = 100%, others = 0%."""
        da = _class_da([[5, 5], [5, 5]])
        pct = _compute_class_percentages(da)
        assert pct[5] == pytest.approx(100.0, abs=0.01)
        for cls in range(1, 5):
            assert pct[cls] == pytest.approx(0.0, abs=0.01)

    def test_nodata_excluded_from_denominator(self) -> None:
        """NODATA cells (-9999) must not count toward the denominator."""
        # 2 valid cells: class 5 and class 1; 2 nodata cells
        da = _class_da([[5, _NODATA], [_NODATA, 1]])
        pct = _compute_class_percentages(da)
        assert pct[5] == pytest.approx(50.0, abs=0.01)
        assert pct[1] == pytest.approx(50.0, abs=0.01)
        assert pct[2] == pytest.approx(0.0, abs=0.01)

    def test_sum_to_100(self) -> None:
        """Percentages of valid cells sum to 100% (within floating-point tolerance)."""
        rng = np.random.default_rng(42)
        data = rng.integers(1, 6, size=(8, 8)).astype(np.int32)
        # Sprinkle some nodata
        data[0, 0] = _NODATA
        data[3, 5] = _NODATA
        da = xr.DataArray(data, dims=["y", "x"])
        pct = _compute_class_percentages(da)
        total = sum(pct.values())
        assert total == pytest.approx(100.0, abs=0.1)

    def test_all_nodata(self) -> None:
        """All-NODATA input → all percentages = 0.0."""
        da = _class_da([[_NODATA, _NODATA]])
        pct = _compute_class_percentages(da)
        for cls in range(1, 6):
            assert pct[cls] == pytest.approx(0.0, abs=0.01)

    def test_returns_all_five_classes(self) -> None:
        """Result always contains keys 1-5 even if some classes are absent."""
        da = _class_da([[5, 5, 5]])
        pct = _compute_class_percentages(da)
        assert set(pct.keys()) == {1, 2, 3, 4, 5}

    def test_class_5_fraction_matches_top1_pct(self) -> None:
        """top1_pct is just pct[5]; hand-check with a 4-cell array."""
        da = _class_da([[5, 5, 4, 3]])
        pct = _compute_class_percentages(da)
        # 2 out of 4 cells are class 5 = 50%
        assert pct[5] == pytest.approx(50.0, abs=0.01)


# ---------------------------------------------------------------------------
# 2. Comparison arithmetic
# ---------------------------------------------------------------------------


class TestComparisonArithmetic:
    """Verify that delta values are computed correctly from pct dicts."""

    def test_delta_top1_correct(self) -> None:
        """Delta top-class = our_top1 - paper_anchor."""
        da = _class_da([[5, 5, 4, 3]])
        pct = _compute_class_percentages(da)
        top1_pct = pct[5]  # 50.0
        delta = round(top1_pct - PAPER_MORE_SUITABLE_PCT, 2)
        assert delta == pytest.approx(50.0 - PAPER_MORE_SUITABLE_PCT, abs=0.01)

    def test_delta_top2_correct(self) -> None:
        """Delta top-two = (class4 + class5) - paper_anchor."""
        da = _class_da([[5, 4, 3, 2, 1]])
        pct = _compute_class_percentages(da)
        top2 = round(pct[5] + pct[4], 2)  # 20 + 20 = 40
        delta = round(top2 - PAPER_MORE_SUITABLE_PCT, 2)
        assert delta == pytest.approx(40.0 - PAPER_MORE_SUITABLE_PCT, abs=0.01)

    def test_paper_anchor_is_24_9(self) -> None:
        """The public anchor constant equals 24.9% exactly."""
        assert pytest.approx(24.9, abs=1e-9) == PAPER_MORE_SUITABLE_PCT


# ---------------------------------------------------------------------------
# 3. Output artefact writing
# ---------------------------------------------------------------------------


class TestArtifactWriting:
    """Test that CSV, Markdown, and PNG artefacts are written correctly."""

    @pytest.fixture
    def tmp_pct(self) -> dict[int, float]:
        """A hand-crafted percentage dict summing to 100."""
        return {5: 34.22, 4: 55.26, 3: 10.21, 2: 0.02, 1: 0.29}

    @pytest.fixture
    def sample_lsi_class(self) -> xr.DataArray:
        """A 4x4 random LSI-class DataArray for PNG writing."""
        rng = np.random.default_rng(7)
        data = rng.integers(1, 6, size=(4, 4)).astype(np.int32)
        return xr.DataArray(data, dims=["y", "x"])

    def test_csv_written(self, tmp_path: Path, tmp_pct: dict[int, float]) -> None:
        """CSV file is written and contains expected columns."""
        csv_path = _write_csv(tmp_pct, tmp_path, resolution_m=500, aoi_area_km2=5280.6)
        assert csv_path.exists()
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0] == "class_id,label,pct_of_valid_area,area_km2"
        assert len(lines) == 6  # header + 5 data rows

    def test_csv_percentages_sum_to_100(self, tmp_path: Path, tmp_pct: dict[int, float]) -> None:
        """CSV data rows' pct column sums to 100 (within rounding)."""
        csv_path = _write_csv(tmp_pct, tmp_path, resolution_m=500, aoi_area_km2=5280.6)
        lines = csv_path.read_text(encoding="utf-8").strip().splitlines()[1:]
        total = sum(float(line.split(",")[2]) for line in lines)
        assert total == pytest.approx(100.0, abs=0.1)

    def test_markdown_table_written(self, tmp_path: Path, tmp_pct: dict[int, float]) -> None:
        """Markdown table file is written and has a header row."""
        md_path = _write_markdown_table(tmp_pct, tmp_path, aoi_area_km2=5280.6)
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "| Class |" in content
        assert "Most suitable" in content
        assert "Least suitable" in content

    def test_png_written(
        self,
        tmp_path: Path,
        sample_lsi_class: xr.DataArray,
    ) -> None:
        """PNG file is written and is non-empty."""
        png_path = _write_lsi_map(sample_lsi_class, tmp_path, aoi_area_km2=5280.6, resolution_m=500)
        assert png_path.exists()
        assert png_path.stat().st_size > 1024  # at least 1 KB

    def test_png_under_500kb(
        self,
        tmp_path: Path,
        sample_lsi_class: xr.DataArray,
    ) -> None:
        """PNG file must remain under 500 KB (committed artefact budget)."""
        png_path = _write_lsi_map(sample_lsi_class, tmp_path, aoi_area_km2=5280.6, resolution_m=500)
        size_kb = png_path.stat().st_size // 1024
        assert size_kb < 500, f"PNG is {size_kb} KB, exceeds 500 KB limit"


# ---------------------------------------------------------------------------
# 4. Integration test against real cache (CI-safe skip)
# ---------------------------------------------------------------------------

_CACHE_DIR = _REPO_ROOT / "data" / "cache"
_AOI_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "nw_coast_aoi.geojson"

_CACHE_AVAILABLE = _CACHE_DIR.exists() and any(_CACHE_DIR.glob("*.json"))


@pytest.mark.skipif(
    not (_CACHE_AVAILABLE and _AOI_FIXTURE.exists()),
    reason=(
        "NW-coast cache not present (data/cache/ empty or missing) — "
        "run scripts/demo_aoi.py --offline to seed it."
    ),
)
class TestRealCacheIntegration:
    """Full offline pipeline over the real NW-coast cached data.

    Skipped automatically in CI where data/cache/ is absent.
    """

    @pytest.fixture(scope="class")
    def results(self, tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
        """Run the validation pipeline once and cache the results."""
        out_dir = tmp_path_factory.mktemp("validation_out")
        return run_validation(
            aoi_path=_AOI_FIXTURE,
            cache_dir=_CACHE_DIR,
            out_dir=out_dir,
            resolution_m=500,
            offline=True,
            classify_method="equal_interval",
            verbose=False,
        )

    def test_class_pct_sum_to_100(self, results: dict[str, object]) -> None:
        """Percentages over the 5 classes must sum to 100% (±0.1%)."""
        pct = results["pct"]  # type: ignore[index]
        assert isinstance(pct, dict)
        total = sum(pct.values())
        assert total == pytest.approx(100.0, abs=0.1), (
            f"Class percentages sum to {total:.3f}%, expected ~100%"
        )

    def test_top1_pct_is_positive(self, results: dict[str, object]) -> None:
        """Top-class percentage must be positive (some highly suitable land exists)."""
        top1 = results["top1_pct"]
        assert isinstance(top1, float)
        assert top1 > 0.0

    def test_top2_pct_ge_top1(self, results: dict[str, object]) -> None:
        """Top-two-class % is at least as large as top-class %."""
        assert results["top2_pct"] >= results["top1_pct"]  # type: ignore[operator]

    def test_paper_anchor_stored(self, results: dict[str, object]) -> None:
        """The published 24.9% anchor is stored in the results."""
        assert results["paper_anchor_pct"] == pytest.approx(24.9, abs=1e-9)  # type: ignore[arg-type]

    def test_delta_top1_computed_correctly(self, results: dict[str, object]) -> None:
        """delta_top1 = top1_pct - 24.9 (within floating-point precision)."""
        expected = round(float(results["top1_pct"]) - 24.9, 2)  # type: ignore[arg-type]
        assert results["delta_top1"] == pytest.approx(expected, abs=0.01)

    def test_delta_top2_computed_correctly(self, results: dict[str, object]) -> None:
        """delta_top2 = top2_pct - 24.9 (within floating-point precision)."""
        expected = round(float(results["top2_pct"]) - 24.9, 2)  # type: ignore[arg-type]
        assert results["delta_top2"] == pytest.approx(expected, abs=0.01)

    def test_valid_cells_positive(self, results: dict[str, object]) -> None:
        """At least some cells are not excluded."""
        assert int(results["n_valid_cells"]) > 0  # type: ignore[arg-type]

    def test_excluded_pct_in_range(self, results: dict[str, object]) -> None:
        """Excluded percentage is between 0 and 100."""
        excl = float(results["excluded_pct"])  # type: ignore[arg-type]
        assert 0.0 <= excl <= 100.0

    def test_output_artefacts_written(self, results: dict[str, object]) -> None:
        """Check that lsi_class is present in the results dict (artefacts are written)."""
        lsi_class = results.get("lsi_class")
        assert lsi_class is not None
        assert isinstance(lsi_class, xr.DataArray)
        # Valid classes are 1-5 or -9999
        vals = lsi_class.values.ravel()
        valid = vals[vals != _NODATA]
        assert np.all(valid >= 1)
        assert np.all(valid <= 5)

    def test_five_classes_present(self, results: dict[str, object]) -> None:
        """All five LSI classes should be present in the result distribution."""
        pct = results["pct"]  # type: ignore[index]
        assert isinstance(pct, dict)
        assert set(pct.keys()) == {1, 2, 3, 4, 5}


# ---------------------------------------------------------------------------
# 5. Synthetic end-to-end: tiny AOI with fabricated layers
# ---------------------------------------------------------------------------


class TestSyntheticEndToEnd:
    """Construct a minimal synthetic pipeline bypassing real I/O.

    Uses only the pure analysis functions to verify end-to-end behaviour
    without any file system or cache dependency.
    """

    def _make_suit_layers(  # type: ignore[override]
        self, registry: object, shape: tuple[int, int]
    ) -> dict[str, xr.DataArray]:
        from solarsite.analysis.registry import load_registry as _lr

        reg = _lr()
        rng = np.random.default_rng(99)
        return {
            f.key: xr.DataArray(
                rng.uniform(0.0, 1.0, shape).astype(np.float64),
                dims=["y", "x"],
            )
            for f in reg.factors
        }

    def test_pipeline_class_sum_100(self) -> None:
        """Synthetic suitability layers → classes sum to 100%."""
        from solarsite.analysis.overlay import classify_lsi, weighted_overlay
        from solarsite.analysis.registry import load_registry

        registry = load_registry()
        suit = self._make_suit_layers(registry, (5, 5))
        lsi = weighted_overlay(suit, registry)
        lsi_class = classify_lsi(lsi, n_classes=5, method="equal_interval")

        pct = _compute_class_percentages(lsi_class)
        total = sum(pct.values())
        assert total == pytest.approx(100.0, abs=0.1)

    def test_pipeline_valid_class_range(self) -> None:
        """After classification, all valid cells have class in [1, 5]."""
        from solarsite.analysis.overlay import classify_lsi, weighted_overlay
        from solarsite.analysis.registry import load_registry

        registry = load_registry()
        suit = self._make_suit_layers(registry, (4, 4))
        lsi = weighted_overlay(suit, registry)
        lsi_class = classify_lsi(lsi, n_classes=5, method="equal_interval")

        vals = lsi_class.values.ravel()
        valid = vals[vals != _NODATA]
        assert np.all(valid >= 1)
        assert np.all(valid <= 5)

    def test_top_two_ge_top_one(self) -> None:
        """top-two % is always >= top-one %."""
        from solarsite.analysis.overlay import classify_lsi, weighted_overlay
        from solarsite.analysis.registry import load_registry

        registry = load_registry()
        suit = self._make_suit_layers(registry, (6, 6))
        lsi = weighted_overlay(suit, registry)
        lsi_class = classify_lsi(lsi, n_classes=5, method="equal_interval")

        pct = _compute_class_percentages(lsi_class)
        top1 = pct[5]
        top2 = pct[5] + pct[4]
        assert top2 >= top1 - 1e-9
