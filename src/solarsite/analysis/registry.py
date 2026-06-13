"""Criteria registry: typed loader and validator for configs/criteria.yaml.

Public API
----------
load_registry(path) -> CriteriaRegistry
CriteriaRegistry
Criterion
CriteriaGroup

The registry validates:
  - group weights sum to 1.0 ± tolerance
  - within each group, local_weight values sum to 1.0 ± tolerance
  - every criterion has a valid kind, data_source, and reclassification spec
  - global weights (= group_weight * local_weight) also sum to 1.0 +/- tolerance
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

# Default path relative to the package root
_DEFAULT_CRITERIA_YAML = Path(__file__).parent.parent.parent.parent / "configs" / "criteria.yaml"

_WEIGHT_TOLERANCE = 1e-6

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class RegistryValidationError(ValueError):
    """Raised when the criteria YAML fails schema or weight-sum validation."""


# ---------------------------------------------------------------------------
# Reclassification sub-models
# ---------------------------------------------------------------------------


class Breakpoint(BaseModel):
    """A single threshold entry in a continuous reclassification table."""

    max: float  # upper bound of this band (math.inf allowed via float)
    score: float = Field(ge=0.0, le=1.0)
    note: str = ""


class BreakpointReclassification(BaseModel):
    """Continuous reclassification: ordered list of upper-bound → score pairs."""

    type: Literal["breakpoints"]
    breakpoints: list[Breakpoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _breakpoints_ordered(self) -> BreakpointReclassification:
        bps = self.breakpoints
        for i in range(len(bps) - 1):
            if bps[i].max >= bps[i + 1].max and not math.isinf(bps[i + 1].max):
                raise RegistryValidationError(
                    f"Breakpoints must be in strictly ascending order; "
                    f"got {bps[i].max} then {bps[i + 1].max}."
                )
        return self


class ClassScoreReclassification(BaseModel):
    """Categorical reclassification: category label/code → suitability score."""

    type: Literal["class_scores"]
    class_scores: dict[str, float] = Field(min_length=1)
    hard_exclusion_classes: list[str] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def _scores_in_range(self) -> ClassScoreReclassification:
        for cls, score in self.class_scores.items():
            if not (0.0 <= score <= 1.0):
                raise RegistryValidationError(f"class_scores['{cls}'] = {score} is outside [0, 1].")
        return self


# Use a discriminated union on the 'type' field
ReclassificationSpec = BreakpointReclassification | ClassScoreReclassification


# ---------------------------------------------------------------------------
# Criterion
# ---------------------------------------------------------------------------


class Criterion(BaseModel):
    """A single evaluation criterion (factor or hard exclusion) with metadata."""

    key: str
    name: str
    group: str
    kind: Literal["factor", "hard_exclusion"]
    local_weight: float = Field(ge=0.0, le=1.0)
    data_source: str
    unit: str = ""
    reclassification: ReclassificationSpec

    @model_validator(mode="after")
    def _validate_consistency(self) -> Criterion:
        if self.kind == "hard_exclusion" and self.local_weight != 0.0:
            # Hard exclusions should not participate in weighted sum
            raise RegistryValidationError(
                f"Criterion '{self.key}' is a hard_exclusion but has "
                f"local_weight={self.local_weight}. Set local_weight to 0."
            )
        return self


# ---------------------------------------------------------------------------
# HardExclusionRule — for standalone hard_exclusion entries in the YAML
# ---------------------------------------------------------------------------


class HardExclusionRule(BaseModel):
    """Binary exclusion rule (not part of the weighted criteria tree)."""

    key: str
    name: str
    kind: Literal["hard_exclusion"]
    data_source: str
    exclude_when: str
    note: str = ""


# ---------------------------------------------------------------------------
# CriteriaGroup
# ---------------------------------------------------------------------------


class CriteriaGroup(BaseModel):
    """A named group with its global weight and the criteria assigned to it."""

    name: str
    weight: float = Field(gt=0.0, le=1.0)
    criteria: list[Criterion] = Field(default_factory=list)

    @property
    def local_weight_sum(self) -> float:
        return sum(c.local_weight for c in self.criteria)


# ---------------------------------------------------------------------------
# CriteriaRegistry — top-level aggregate
# ---------------------------------------------------------------------------


class CriteriaRegistry(BaseModel):
    """Validated registry of all criteria groups, criteria, and exclusion rules."""

    groups: dict[str, CriteriaGroup]
    lsi_classes: list[dict[str, Any]] = Field(default_factory=list)
    hard_exclusion_rules: list[HardExclusionRule] = Field(default_factory=list)

    # All factor criteria keyed by criterion key, populated during validation
    _criteria_index: dict[str, Criterion] = {}

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _validate_weights(self) -> CriteriaRegistry:
        # 1. Group weights must sum to 1.0
        group_sum = sum(g.weight for g in self.groups.values())
        if abs(group_sum - 1.0) > _WEIGHT_TOLERANCE:
            raise RegistryValidationError(
                f"Group weights sum to {group_sum:.8f}, expected 1.0 "
                f"(tolerance ±{_WEIGHT_TOLERANCE})."
            )

        # 2. Within each group, local weights of factor criteria must sum to 1.0
        for gkey, group in self.groups.items():
            factor_criteria = [c for c in group.criteria if c.kind == "factor"]
            if factor_criteria:
                lw_sum = sum(c.local_weight for c in factor_criteria)
                if abs(lw_sum - 1.0) > _WEIGHT_TOLERANCE:
                    raise RegistryValidationError(
                        f"Group '{gkey}' local weights sum to {lw_sum:.8f}, "
                        f"expected 1.0 (tolerance ±{_WEIGHT_TOLERANCE})."
                    )

        # 3. Global weights must sum to 1.0
        global_sum = self._sum_global_weights()
        if abs(global_sum - 1.0) > _WEIGHT_TOLERANCE:
            raise RegistryValidationError(
                f"Global weights (group_weight * local_weight) sum to "
                f"{global_sum:.8f}, expected 1.0."
            )

        # 4. Build index
        self._criteria_index = {
            c.key: c for g in self.groups.values() for c in g.criteria if c.kind == "factor"
        }

        return self

    def _sum_global_weights(self) -> float:
        total = 0.0
        for group in self.groups.values():
            for c in group.criteria:
                if c.kind == "factor":
                    total += group.weight * c.local_weight
        return total

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def factors(self) -> list[Criterion]:
        """Return all factor criteria across all groups (ordered by group, then criterion)."""
        return [c for group in self.groups.values() for c in group.criteria if c.kind == "factor"]

    @property
    def hard_exclusions(self) -> list[HardExclusionRule]:
        """Return the standalone hard-exclusion rules."""
        return list(self.hard_exclusion_rules)

    def global_weight(self, criterion_key: str) -> float:
        """Return group_weight * local_weight for a criterion key.

        Raises KeyError if the key is not found among factor criteria.
        """
        for group in self.groups.values():
            for c in group.criteria:
                if c.key == criterion_key and c.kind == "factor":
                    return group.weight * c.local_weight
        raise KeyError(f"Criterion '{criterion_key}' not found in registry factors.")

    def all_global_weights(self) -> dict[str, float]:
        """Return {criterion_key: global_weight} for every factor criterion."""
        result: dict[str, float] = {}
        for group in self.groups.values():
            for c in group.criteria:
                if c.kind == "factor":
                    result[c.key] = group.weight * c.local_weight
        return result

    def criterion(self, key: str) -> Criterion:
        """Look up a factor criterion by key; raises KeyError if absent."""
        for group in self.groups.values():
            for c in group.criteria:
                if c.key == key:
                    return c
        raise KeyError(f"Criterion '{key}' not found in registry.")


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _parse_reclassification(raw: dict[str, Any]) -> ReclassificationSpec:
    """Parse a raw dict into a typed reclassification spec."""
    rtype = raw.get("type")
    if rtype == "breakpoints":
        return BreakpointReclassification.model_validate(raw)
    elif rtype == "class_scores":
        return ClassScoreReclassification.model_validate(raw)
    else:
        raise RegistryValidationError(
            f"Unknown reclassification type '{rtype}'. Expected 'breakpoints' or 'class_scores'."
        )


def _parse_criterion(key: str, raw: dict[str, Any]) -> Criterion:
    """Parse a raw criterion dict, resolving inf values and reclassification."""
    reclass_raw = raw.get("reclassification")
    if reclass_raw is None:
        raise RegistryValidationError(f"Criterion '{key}' is missing a 'reclassification' section.")
    reclass = _parse_reclassification(reclass_raw)

    return Criterion(
        key=key,
        name=raw["name"],
        group=raw["group"],
        kind=raw["kind"],
        local_weight=float(raw["local_weight"]),
        data_source=raw["data_source"],
        unit=raw.get("unit", ""),
        reclassification=reclass,
    )


def load_registry(path: Path | str | None = None) -> CriteriaRegistry:
    """Load and validate the criteria registry from a YAML file.

    Parameters
    ----------
    path:
        Path to the criteria YAML.  Defaults to
        ``<repo_root>/configs/criteria.yaml``.

    Returns
    -------
    CriteriaRegistry
        Fully validated registry ready for use by the AHP module and overlay
        engine.

    Raises
    ------
    RegistryValidationError
        If any structural or weight-sum constraint is violated.
    FileNotFoundError
        If the YAML file does not exist at the resolved path.
    """
    resolved = Path(path) if path is not None else _DEFAULT_CRITERIA_YAML

    if not resolved.exists():
        raise FileNotFoundError(
            f"Criteria YAML not found at '{resolved}'. "
            "Ensure configs/criteria.yaml exists in the repository root."
        )

    with resolved.open("r", encoding="utf-8") as fh:
        raw_doc: dict[str, Any] = yaml.safe_load(fh)

    if not isinstance(raw_doc, dict):
        raise RegistryValidationError("YAML root must be a mapping.")

    # --- Parse groups -------------------------------------------------------
    raw_groups: dict[str, Any] = raw_doc.get("groups", {})
    if not raw_groups:
        raise RegistryValidationError("YAML must contain a non-empty 'groups' section.")

    groups: dict[str, CriteriaGroup] = {}
    for gkey, gdata in raw_groups.items():
        groups[gkey] = CriteriaGroup(
            name=gdata["name"],
            weight=float(gdata["weight"]),
            criteria=[],
        )

    # --- Parse criteria and assign to groups --------------------------------
    raw_criteria: dict[str, Any] = raw_doc.get("criteria", {})
    for ckey, cdata in raw_criteria.items():
        criterion = _parse_criterion(ckey, cdata)
        gkey = criterion.group
        if gkey not in groups:
            raise RegistryValidationError(
                f"Criterion '{ckey}' references unknown group '{gkey}'. "
                f"Defined groups: {list(groups.keys())}."
            )
        groups[gkey].criteria.append(criterion)

    # --- Parse hard-exclusion rules -----------------------------------------
    raw_excl: list[dict[str, Any]] = raw_doc.get("hard_exclusions", [])
    hard_exclusion_rules: list[HardExclusionRule] = [
        HardExclusionRule.model_validate(e) for e in raw_excl
    ]

    # --- Parse LSI classes --------------------------------------------------
    lsi_classes: list[dict[str, Any]] = raw_doc.get("lsi_classes", [])

    # --- Assemble and validate CriteriaRegistry ----------------------------
    try:
        registry = CriteriaRegistry(
            groups=groups,
            lsi_classes=lsi_classes,
            hard_exclusion_rules=hard_exclusion_rules,
        )
    except (RegistryValidationError, ValueError) as exc:
        raise RegistryValidationError(str(exc)) from exc

    return registry
