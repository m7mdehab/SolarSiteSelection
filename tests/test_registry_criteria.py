"""Tests for the criteria registry: loader, schema validation, weight arithmetic.

Covers:
- configs/criteria.yaml loads without error
- Group weights sum to 1.0
- Per-group local weights sum to 1.0
- All global weights sum to 1.0
- Every expected sub-criterion is present with required attributes
- Hard exclusions are flagged and retrievable
- Malformed registry YAML raises RegistryValidationError
- .global_weight() math is correct
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solarsite.analysis.registry import (
    CriteriaRegistry,
    HardExclusionRule,
    RegistryValidationError,
    load_registry,
)

# ---------------------------------------------------------------------------
# Path to the shipped YAML
# ---------------------------------------------------------------------------

_CRITERIA_YAML = Path(__file__).parent.parent / "configs" / "criteria.yaml"

_WEIGHT_TOL = 1e-6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_default() -> CriteriaRegistry:
    return load_registry(_CRITERIA_YAML)


def _registry_from_yaml(text: str) -> CriteriaRegistry:
    """Write inline YAML to a tmp structure and load it via load_registry."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(textwrap.dedent(text))
        tmp_path = Path(fh.name)
    try:
        return load_registry(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. Default YAML loads without error
# ---------------------------------------------------------------------------


def test_default_yaml_loads() -> None:
    registry = _load_default()
    assert isinstance(registry, CriteriaRegistry)


# ---------------------------------------------------------------------------
# 2. Weight sums
# ---------------------------------------------------------------------------


def test_group_weights_sum_to_one() -> None:
    registry = _load_default()
    total = sum(g.weight for g in registry.groups.values())
    assert abs(total - 1.0) < _WEIGHT_TOL, f"Group weights sum = {total}"


def test_per_group_local_weights_sum_to_one() -> None:
    registry = _load_default()
    for gkey, group in registry.groups.items():
        factor_criteria = [c for c in group.criteria if c.kind == "factor"]
        if not factor_criteria:
            continue
        lw_sum = sum(c.local_weight for c in factor_criteria)
        assert abs(lw_sum - 1.0) < _WEIGHT_TOL, f"Group '{gkey}' local weights sum = {lw_sum}"


def test_global_weights_sum_to_one() -> None:
    registry = _load_default()
    gw = registry.all_global_weights()
    total = sum(gw.values())
    assert abs(total - 1.0) < _WEIGHT_TOL, f"Global weights sum = {total}"


# ---------------------------------------------------------------------------
# 3. All required sub-criteria present with required attributes
# ---------------------------------------------------------------------------

_REQUIRED_CRITERIA = [
    "solar_radiation",
    "slope",
    "aspect",
    "shadow",
    "dist_ptl",
    "dist_roads",
    "dist_railway",
    "dist_urban",
    "lulc",
    "temperature",
    "humidity",
    "wind_speed",
    "land_capability",
]


@pytest.mark.parametrize("ckey", _REQUIRED_CRITERIA)
def test_required_criterion_present(ckey: str) -> None:
    registry = _load_default()
    criterion = registry.criterion(ckey)
    assert criterion.key == ckey
    assert criterion.group in registry.groups
    assert criterion.kind in ("factor", "hard_exclusion")
    assert criterion.data_source, f"Criterion '{ckey}' has empty data_source"
    assert criterion.reclassification is not None


@pytest.mark.parametrize("ckey", _REQUIRED_CRITERIA)
def test_required_criterion_has_reclassification(ckey: str) -> None:
    registry = _load_default()
    criterion = registry.criterion(ckey)
    reclass = criterion.reclassification
    # Must be one of the two valid types
    assert reclass.type in ("breakpoints", "class_scores")


# ---------------------------------------------------------------------------
# 4. Hard exclusions retrievable
# ---------------------------------------------------------------------------


def test_hard_exclusion_rules_present() -> None:
    registry = _load_default()
    excls = registry.hard_exclusions
    assert len(excls) >= 1, "No hard-exclusion rules found"
    for rule in excls:
        assert isinstance(rule, HardExclusionRule)
        assert rule.kind == "hard_exclusion"
        assert rule.key
        assert rule.data_source
        assert rule.exclude_when


def test_hard_exclusion_keys_include_expected() -> None:
    registry = _load_default()
    keys = {e.key for e in registry.hard_exclusions}
    expected = {
        "excl_urban_buffer",  # PUBLIC < 1.5 km
        "excl_road_buffer",  # PUBLIC 150 m
        "excl_railway_buffer",  # PUBLIC 150 m
        "excl_ptl_buffer",  # PUBLIC 4.8 km
        "excl_wdpa",
        "excl_water",
        "excl_urban_core",
        "excl_slope",
    }
    assert expected <= keys, f"Missing exclusion keys: {expected - keys}"


def test_factors_vs_hard_exclusions_separation() -> None:
    registry = _load_default()
    factor_keys = {c.key for c in registry.factors}
    # Standalone hard-exclusion rules are separate from factor criteria
    excl_keys = {e.key for e in registry.hard_exclusions}
    overlap = factor_keys & excl_keys
    assert not overlap, f"Keys appear in both factors and hard_exclusions: {overlap}"


# ---------------------------------------------------------------------------
# 5. .global_weight() math
# ---------------------------------------------------------------------------


def test_global_weight_solar_radiation() -> None:
    registry = _load_default()
    # solar_radiation: group=technical (0.25), local_weight=0.40
    gw = registry.global_weight("solar_radiation")
    expected = 0.25 * 0.40
    assert abs(gw - expected) < _WEIGHT_TOL, f"global_weight = {gw}, expected {expected}"


def test_global_weight_dist_ptl() -> None:
    registry = _load_default()
    # dist_ptl: group=economic (0.50), local_weight=0.30
    gw = registry.global_weight("dist_ptl")
    expected = 0.50 * 0.30
    assert abs(gw - expected) < _WEIGHT_TOL, f"global_weight = {gw}, expected {expected}"


def test_global_weight_lulc() -> None:
    registry = _load_default()
    # lulc: group=environmental (0.25), local_weight=0.40
    gw = registry.global_weight("lulc")
    expected = 0.25 * 0.40
    assert abs(gw - expected) < _WEIGHT_TOL


def test_global_weight_missing_key_raises() -> None:
    registry = _load_default()
    with pytest.raises(KeyError, match="nonexistent_key"):
        registry.global_weight("nonexistent_key")


# ---------------------------------------------------------------------------
# 6. Malformed registries raise RegistryValidationError
# ---------------------------------------------------------------------------


_MINIMAL_GOOD_YAML = """\
    groups:
      economic:
        name: Economic
        weight: 0.50
      technical:
        name: Technical
        weight: 0.25
      environmental:
        name: Environmental
        weight: 0.25

    lsi_classes: []
    hard_exclusions: []

    criteria:
      solar_radiation:
        key: solar_radiation
        name: Solar Radiation
        group: technical
        kind: factor
        local_weight: 1.0
        data_source: pvgis
        unit: kWh/m2/day
        reclassification:
          type: breakpoints
          breakpoints:
            - max: 5.0
              score: 0.80
            - max: .inf
              score: 1.0
      dist_ptl:
        key: dist_ptl
        name: Distance to PTL
        group: economic
        kind: factor
        local_weight: 1.0
        data_source: osm_power
        unit: km
        reclassification:
          type: breakpoints
          breakpoints:
            - max: 10.0
              score: 1.0
            - max: .inf
              score: 0.5
      lulc:
        key: lulc
        name: LULC
        group: environmental
        kind: factor
        local_weight: 1.0
        data_source: worldcover
        unit: class
        reclassification:
          type: class_scores
          class_scores:
            "60": 1.0
            "50": 0.0
    """


def test_good_minimal_yaml_loads() -> None:
    registry = _registry_from_yaml(_MINIMAL_GOOD_YAML)
    assert isinstance(registry, CriteriaRegistry)


def test_group_weights_not_summing_raises() -> None:
    bad = _MINIMAL_GOOD_YAML.replace("weight: 0.50", "weight: 0.60")
    with pytest.raises(RegistryValidationError, match="Group weights sum"):
        _registry_from_yaml(bad)


def test_local_weights_not_summing_raises() -> None:
    # Make technical group have local_weight = 0.5 (not 1.0)
    bad = _MINIMAL_GOOD_YAML.replace(
        "group: technical\n        kind: factor\n        local_weight: 1.0",
        "group: technical\n        kind: factor\n        local_weight: 0.5",
    )
    with pytest.raises(RegistryValidationError):
        _registry_from_yaml(bad)


def test_missing_reclassification_raises() -> None:
    # Remove the reclassification block from solar_radiation
    lines = textwrap.dedent(_MINIMAL_GOOD_YAML).splitlines()
    # Drop lines containing 'reclassification', 'type: breakpoints', 'breakpoints:', and 'max:'
    filtered = [
        line
        for line in lines
        if "reclassification" not in line
        and "type: breakpoints" not in line
        and "breakpoints:" not in line
        and "- max:" not in line
        and "score:" not in line
    ]
    bad = "\n".join(filtered)
    # This should fail because reclassification is missing
    with pytest.raises((RegistryValidationError, KeyError, Exception)):
        _registry_from_yaml(bad)


def test_bad_kind_raises() -> None:
    bad = _MINIMAL_GOOD_YAML.replace("kind: factor", "kind: invalid_kind", 1)
    with pytest.raises((RegistryValidationError, ValueError)):
        _registry_from_yaml(bad)


def test_missing_groups_raises() -> None:
    # No groups section at all
    bad = textwrap.dedent("""\
        criteria: {}
        lsi_classes: []
        hard_exclusions: []
    """)
    with pytest.raises(RegistryValidationError, match="groups"):
        _registry_from_yaml(bad)


def test_criterion_unknown_group_raises() -> None:
    bad = _MINIMAL_GOOD_YAML.replace("group: technical", "group: nonexistent", 1)
    with pytest.raises(RegistryValidationError, match="unknown group"):
        _registry_from_yaml(bad)


def test_nonexistent_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_registry(Path("/tmp/does_not_exist_xyz123.yaml"))


# ---------------------------------------------------------------------------
# 7. .factors helper
# ---------------------------------------------------------------------------


def test_factors_returns_only_factor_kind() -> None:
    registry = _load_default()
    for criterion in registry.factors:
        assert criterion.kind == "factor"


def test_factors_includes_all_required() -> None:
    registry = _load_default()
    factor_keys = {c.key for c in registry.factors}
    for ckey in _REQUIRED_CRITERIA:
        assert ckey in factor_keys, f"'{ckey}' missing from .factors"


# ---------------------------------------------------------------------------
# 8. LSI classes
# ---------------------------------------------------------------------------


def test_lsi_classes_present() -> None:
    registry = _load_default()
    assert len(registry.lsi_classes) == 5, (
        f"Expected 5 LSI classes, got {len(registry.lsi_classes)}"
    )


def test_lsi_class_ids_cover_1_to_5() -> None:
    registry = _load_default()
    ids = {c["id"] for c in registry.lsi_classes}
    assert ids == {1, 2, 3, 4, 5}


# ---------------------------------------------------------------------------
# 9. Breakpoint reclassification properties
# ---------------------------------------------------------------------------


def test_slope_breakpoints_start_with_five_degrees() -> None:
    """slope < 5° must be score 1.0 (PUBLIC — Habib 2020 abstract)."""
    registry = _load_default()
    slope = registry.criterion("slope")
    assert slope.reclassification.type == "breakpoints"
    first_bp = slope.reclassification.breakpoints[0]  # type: ignore[union-attr]
    assert first_bp.max == pytest.approx(5.0)
    assert first_bp.score == pytest.approx(1.0)


def test_dist_ptl_safety_buffer_score_zero() -> None:
    """PTL safety buffer < 4.8 km must have score 0 (PUBLIC — Habib 2020)."""
    registry = _load_default()
    ptl = registry.criterion("dist_ptl")
    assert ptl.reclassification.type == "breakpoints"
    first_bp = ptl.reclassification.breakpoints[0]  # type: ignore[union-attr]
    assert first_bp.max == pytest.approx(4.8)
    assert first_bp.score == pytest.approx(0.0)


def test_dist_urban_exclusion_at_1_5_km() -> None:
    """Urban exclusion buffer < 1.5 km must score 0 (PUBLIC — Habib 2020)."""
    registry = _load_default()
    urban = registry.criterion("dist_urban")
    assert urban.reclassification.type == "breakpoints"
    first_bp = urban.reclassification.breakpoints[0]  # type: ignore[union-attr]
    assert first_bp.max == pytest.approx(1.5)
    assert first_bp.score == pytest.approx(0.0)


def test_lulc_class_scores_in_range() -> None:
    registry = _load_default()
    lulc = registry.criterion("lulc")
    assert lulc.reclassification.type == "class_scores"
    for cls, score in lulc.reclassification.class_scores.items():  # type: ignore[union-attr]
        assert 0.0 <= score <= 1.0, f"LULC class '{cls}' score {score} out of [0,1]"


def test_solar_radiation_ghi_range_public() -> None:
    """Check that the study-area GHI bounds (4.7 to 5.9) appear in breakpoints (PUBLIC)."""
    registry = _load_default()
    rad = registry.criterion("solar_radiation")
    assert rad.reclassification.type == "breakpoints"
    bp_maxes = [bp.max for bp in rad.reclassification.breakpoints]  # type: ignore[union-attr]
    assert 4.7 in bp_maxes, "4.7 kWh/m2/day lower bound not found in solar_radiation breakpoints"
    assert 5.9 in bp_maxes, "5.9 kWh/m2/day upper bound not found in solar_radiation breakpoints"
