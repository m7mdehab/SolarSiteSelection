"""FastAPI application — SolarSiteSelection P3.1 API.

Routes
------
GET  /criteria                       → full criteria registry for UI rendering
POST /ahp/check                      → AHP weight computation + consistency check
POST /analyze                        → submit analysis job
GET  /jobs/{id}                      → staged progress (real pipeline bar)
GET  /jobs/{id}/layers/{name}.png    → colormapped layer PNG
GET  /jobs/{id}/layers/{name}.bounds → WGS-84 bounding box JSON
GET  /jobs/{id}/sites.geojson        → ranked candidate sites (WGS-84)
GET  /jobs/{id}/report.pdf           → PDF report (501 until P3.3)

The app exposes a ``_registry`` attribute (JobRegistry instance) so tests can
inject a custom layer provider without patching module globals.
"""

from __future__ import annotations

import json
import logging

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from solarsite.analysis import AHPResult, ahp_weights
from solarsite.analysis.ahp import AHPError
from solarsite.analysis.registry import (
    BreakpointReclassification,
    ClassScoreReclassification,
    load_registry,
)
from solarsite.api.jobs import JobRegistry, LayerProviderFn
from solarsite.api.render import render_report
from solarsite.api.schemas import (
    AHPCheckRequest,
    AHPCheckResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    BreakpointOut,
    CriteriaGroupOut,
    CriteriaResponse,
    CriterionOut,
    HardExclusionRuleOut,
    JobState,
    JobStatus,
    LsiClassOut,
    ReclassOut,
)
from solarsite.core import AOI, AOIInvalidGeometryError, AOITooLargeError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SolarSiteSelection API",
    version="0.1.0",
    description="GIS-based multi-criteria PV site selection API.",
)

# Shared job registry (one per process lifetime).
# Tests access app._registry to inject a synthetic layer provider.
app._registry: JobRegistry = JobRegistry()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# GET /criteria
# ---------------------------------------------------------------------------


@app.get("/criteria", response_model=CriteriaResponse)
def get_criteria() -> CriteriaResponse:
    """Return the full criteria registry serialised for UI rendering.

    Groups (with their weights), criteria (with local + global weights,
    reclassification tables, units), LSI class labels, and hard-exclusion
    rules are all included so the frontend can render the criteria editor
    without a separate config fetch.
    """
    registry = load_registry()

    groups_out: dict[str, CriteriaGroupOut] = {}
    for gkey, group in registry.groups.items():
        criteria_out: list[CriterionOut] = []
        for c in group.criteria:
            if c.kind != "factor":
                continue
            reclass = c.reclassification
            if isinstance(reclass, BreakpointReclassification):
                reclass_out = ReclassOut(
                    type="breakpoints",
                    breakpoints=[
                        BreakpointOut(max=bp.max, score=bp.score, note=bp.note)
                        for bp in reclass.breakpoints
                    ],
                )
            elif isinstance(reclass, ClassScoreReclassification):
                reclass_out = ReclassOut(
                    type="class_scores",
                    class_scores=dict(reclass.class_scores),
                    hard_exclusion_classes=list(reclass.hard_exclusion_classes),
                    note=reclass.note,
                )
            else:
                reclass_out = ReclassOut(type=str(type(reclass).__name__))

            criteria_out.append(
                CriterionOut(
                    key=c.key,
                    name=c.name,
                    group=c.group,
                    kind=c.kind,
                    local_weight=c.local_weight,
                    global_weight=registry.global_weight(c.key),
                    data_source=c.data_source,
                    unit=c.unit,
                    reclassification=reclass_out,
                )
            )

        groups_out[gkey] = CriteriaGroupOut(
            name=group.name,
            weight=group.weight,
            criteria=criteria_out,
        )

    lsi_classes_out = [
        LsiClassOut(
            id=int(lc["id"]),
            label=str(lc.get("label", "")),
            description=str(lc.get("description", "")),
        )
        for lc in registry.lsi_classes
    ]

    exclusion_rules_out = [
        HardExclusionRuleOut(
            key=r.key,
            name=r.name,
            kind=r.kind,
            data_source=r.data_source,
            exclude_when=r.exclude_when,
            note=r.note,
        )
        for r in registry.hard_exclusion_rules
    ]

    return CriteriaResponse(
        groups=groups_out,
        lsi_classes=lsi_classes_out,
        hard_exclusion_rules=exclusion_rules_out,
    )


# ---------------------------------------------------------------------------
# POST /ahp/check
# ---------------------------------------------------------------------------


@app.post("/ahp/check", response_model=AHPCheckResponse)
def ahp_check(body: AHPCheckRequest) -> AHPCheckResponse:
    """Compute AHP weights and consistency metrics from a pairwise matrix.

    Returns weights, lambda_max, CI, CR, and whether the matrix is
    consistent (CR ≤ 0.10).  A non-square or degenerate matrix returns 422.
    """
    matrix_raw = body.matrix
    # --- basic structural validation ----------------------------------------
    n = len(matrix_raw)
    if n < 2:
        raise HTTPException(
            status_code=422,
            detail="Matrix must have at least 2 rows (n ≥ 2).",
        )
    for i, row in enumerate(matrix_raw):
        if len(row) != n:
            raise HTTPException(
                status_code=422,
                detail=f"Matrix is not square: row {i} has {len(row)} elements, expected {n}.",
            )

    try:
        mat = np.array(matrix_raw, dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid matrix values: {exc}") from exc

    if not np.all(mat > 0):
        raise HTTPException(
            status_code=422,
            detail="All matrix entries must be positive (Saaty 1-9 scale).",
        )

    try:
        result: AHPResult = ahp_weights(mat)
    except AHPError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"AHP computation failed: {exc}") from exc

    return AHPCheckResponse(
        weights=result.weights.tolist(),
        lambda_max=float(result.lambda_max),
        ci=float(result.ci),
        cr=float(result.cr),
        consistent=bool(result.consistent),
        most_inconsistent=result.most_inconsistent,
    )


# ---------------------------------------------------------------------------
# POST /analyze
# ---------------------------------------------------------------------------


@app.post("/analyze", response_model=AnalyzeResponse, status_code=202)
async def analyze(body: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    """Submit an analysis job.

    Validates the AOI (must be a valid WGS-84 GeoJSON geometry ≤ 10 000 km²),
    creates a job, starts it on the in-process queue, and immediately returns
    the job_id so the client can poll GET /jobs/{id}.

    An optional ``layer_provider`` can be injected into ``request.state`` by
    tests (via middleware or app state) to supply synthetic layers without
    touching the network.
    """
    # --- parse + validate AOI ------------------------------------------------
    try:
        aoi = AOI.from_geojson(body.aoi)
    except AOIInvalidGeometryError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid AOI geometry: {exc}") from exc
    except AOITooLargeError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"AOI too large: {exc.area_km2:.1f} km² exceeds limit of {exc.max_km2:.0f} km²."
            ),
        ) from exc

    # --- optional injected provider (for testing) ----------------------------
    layer_provider: LayerProviderFn | None = getattr(request.state, "layer_provider", None)

    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    job_id = registry.submit(aoi, body.resolution_m, body.weight_overrides, layer_provider)

    return AnalyzeResponse(job_id=job_id, status=JobStatus.queued)


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}", response_model=JobState)
def get_job(job_id: str) -> JobState:
    """Return the full staged progress for a job.

    The response includes ``acquire_stages`` (one per acquire source) and
    ``analysis_status`` so the UI can show a real pipeline progress view.
    Returns 404 for unknown job ids.
    """
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    record = registry.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return record.to_job_state()


# ---------------------------------------------------------------------------
# GET /jobs/{id}/layers/{name}.png
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/layers/{layer_name}.png")
def get_layer_png(job_id: str, layer_name: str) -> Response:
    """Return a colormapped PNG for a named layer.

    The WGS-84 bounding box is returned in the ``X-Layer-Bounds`` response
    header as a JSON string ``{"west":…,"south":…,"east":…,"north":…}`` so
    the frontend can place the overlay on a map without an extra request.

    Returns 404 for unknown jobs, 409 if the job is not yet done, and 404 if
    the requested layer does not exist.
    """
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    record = registry.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if record.status not in (JobStatus.done, JobStatus.error):
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not yet done (status={record.status.value}).",
        )
    if record.status == JobStatus.error:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' failed: {record.error}",
        )

    job_dir = record.job_dir
    png_path = job_dir / f"{layer_name}.png"
    if not png_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Layer '{layer_name}' not found for job '{job_id}'.",
        )

    png_bytes = png_path.read_bytes()
    headers: dict[str, str] = {}
    bounds_path = job_dir / f"{layer_name}.bounds.json"
    if bounds_path.exists():
        headers["X-Layer-Bounds"] = bounds_path.read_text(encoding="utf-8")

    return Response(content=png_bytes, media_type="image/png", headers=headers)


# ---------------------------------------------------------------------------
# GET /jobs/{id}/layers/{name}.bounds  (sibling endpoint for bounds JSON)
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/layers/{layer_name}.bounds")
def get_layer_bounds(job_id: str, layer_name: str) -> JSONResponse:
    """Return the WGS-84 bounding box for a layer as JSON.

    Response: ``{"west": float, "south": float, "east": float, "north": float}``

    This is the sibling of the PNG endpoint and allows the frontend to place
    the overlay on a map without parsing a response header.
    """
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    record = registry.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if record.status != JobStatus.done:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not yet done (status={record.status.value}).",
        )

    bounds_path = record.job_dir / f"{layer_name}.bounds.json"
    if not bounds_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Bounds for layer '{layer_name}' not found for job '{job_id}'.",
        )
    return JSONResponse(content=json.loads(bounds_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# GET /jobs/{id}/sites.geojson
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/sites.geojson")
def get_sites(job_id: str) -> Response:
    """Return ranked candidate sites as WGS-84 GeoJSON.

    Each feature has properties: rank, area_km2, mean_lsi, max_lsi,
    centroid_lon, centroid_lat, and energy fields (if computed).

    Returns 404 for unknown jobs, 409 if the job is not done yet.
    """
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    record = registry.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if record.status not in (JobStatus.done, JobStatus.error):
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not yet done (status={record.status.value}).",
        )
    if record.status == JobStatus.error:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' failed: {record.error}",
        )

    geojson_path = record.job_dir / "sites.geojson"
    if not geojson_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Sites not found for job '{job_id}'.",
        )

    return Response(
        content=geojson_path.read_text(encoding="utf-8"),
        media_type="application/geo+json",
    )


# ---------------------------------------------------------------------------
# GET /jobs/{id}/report.pdf
# ---------------------------------------------------------------------------


@app.get("/jobs/{job_id}/report.pdf")
def get_report(job_id: str) -> Response:
    """Return the PDF analysis report.

    Returns 501 Not Implemented until P3.3 wires in the WeasyPrint renderer.
    The renderer is pluggable: P3.3 replaces ``solarsite.api.render.render_report``
    with its own implementation; no other file changes are needed.
    """
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    record = registry.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if record.status != JobStatus.done:
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not yet done (status={record.status.value}).",
        )

    try:
        pdf_bytes = render_report(job_id, record.job_dir)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=(
                "PDF report rendering is not yet implemented. "
                "P3.3 will replace the render_report stub in solarsite/api/render.py. "
                f"Details: {exc}"
            ),
        ) from exc

    return Response(content=pdf_bytes, media_type="application/pdf")
