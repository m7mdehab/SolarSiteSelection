"""Pydantic request/response schemas for the SolarSiteSelection API.

All public routes have typed request/response models defined here so the
OpenAPI schema is accurate and the job state machine is easy to follow.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    """Top-level job lifecycle status."""

    queued = "queued"
    acquiring = "acquiring"
    analyzing = "analyzing"
    done = "done"
    error = "error"


class StageStatus(StrEnum):
    """Per-source / per-stage granular status (drives the real progress bar)."""

    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


# ---------------------------------------------------------------------------
# /POST analyze — request
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Request body for POST /analyze."""

    aoi: dict[str, Any] = Field(
        ...,
        description=(
            "GeoJSON object — Polygon, MultiPolygon, Feature, or FeatureCollection "
            "in WGS-84. Area must be ≤ 10 000 km²."
        ),
    )
    resolution_m: int = Field(
        500,
        ge=50,
        le=5000,
        description="Grid resolution in metres (50-5000). Default 500.",
    )
    weight_overrides: dict[str, float] | None = Field(
        None,
        description=(
            "Optional per-criterion weight overrides keyed by criterion_key. "
            "Weights are renormalised within each group so only the ratios matter."
        ),
    )


class AnalyzeResponse(BaseModel):
    """Immediate response from POST /analyze."""

    job_id: str
    status: JobStatus


# ---------------------------------------------------------------------------
# GET /jobs/{id} — staged progress
# ---------------------------------------------------------------------------


class AcquireSourceStage(BaseModel):
    """Per-acquire-source progress entry."""

    source: str = Field(..., description="Source label (e.g. 'solar', 'terrain', 'lulc').")
    status: StageStatus
    error: str | None = None


class JobState(BaseModel):
    """Full job state returned by GET /jobs/{id}."""

    job_id: str
    status: JobStatus
    resolution_m: int

    # --- per-stage breakdown (drives real pipeline progress UI) ---------------
    acquire_stages: list[AcquireSourceStage] = Field(
        default_factory=list,
        description="One entry per acquire source; ordered pipeline sequence.",
    )
    analysis_status: StageStatus = StageStatus.pending
    analysis_error: str | None = None

    # --- error detail (set on top-level status=error) -------------------------
    error: str | None = None

    # --- result summary (populated once status=done) --------------------------
    n_sites: int | None = None
    skipped_sources: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /ahp/check — request/response
# ---------------------------------------------------------------------------


class AHPCheckRequest(BaseModel):
    """Pairwise comparison matrix for AHP weight computation."""

    matrix: list[list[float]] = Field(
        ...,
        description=(
            "Square positive-reciprocal pairwise matrix on the Saaty 1-9 scale. "
            "Must be nxn with n >= 2."
        ),
    )


class AHPCheckResponse(BaseModel):
    """AHP weight computation result."""

    weights: list[float] = Field(..., description="Normalised weights, sum ≈ 1.")
    lambda_max: float
    ci: float = Field(..., description="Consistency Index.")
    cr: float = Field(..., description="Consistency Ratio.")
    consistent: bool = Field(..., description="True iff CR ≤ 0.10.")
    most_inconsistent: tuple[int, int, int] | None = Field(
        None,
        description=(
            "Indices (i, j, k) of the most inconsistent triplet, or null if "
            "the matrix is consistent."
        ),
    )


# ---------------------------------------------------------------------------
# GET /criteria — response (serialises the registry)
# ---------------------------------------------------------------------------


class BreakpointOut(BaseModel):
    max: float
    score: float
    note: str = ""


class ReclassOut(BaseModel):
    type: str
    breakpoints: list[BreakpointOut] | None = None
    class_scores: dict[str, float] | None = None
    hard_exclusion_classes: list[str] | None = None
    note: str = ""


class CriterionOut(BaseModel):
    key: str
    name: str
    group: str
    kind: str
    local_weight: float
    global_weight: float
    data_source: str
    unit: str
    reclassification: ReclassOut


class CriteriaGroupOut(BaseModel):
    name: str
    weight: float
    criteria: list[CriterionOut]


class LsiClassOut(BaseModel):
    id: int
    label: str
    description: str = ""


class HardExclusionRuleOut(BaseModel):
    key: str
    name: str
    kind: str
    data_source: str
    exclude_when: str
    note: str = ""


class CriteriaResponse(BaseModel):
    """Full registry serialised for UI rendering."""

    groups: dict[str, CriteriaGroupOut]
    lsi_classes: list[LsiClassOut]
    hard_exclusion_rules: list[HardExclusionRuleOut]
