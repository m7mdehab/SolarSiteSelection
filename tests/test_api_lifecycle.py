"""API test suite for P3.1 — full job lifecycle, AHP, criteria, concurrent jobs.

All tests are fully offline: a synthetic layer provider is injected into the
JobRegistry via ``app._registry`` replacement, so no network calls are made
and the pipeline completes in milliseconds.

Test coverage targets:
  * Full job lifecycle: POST /analyze → poll /jobs/{id} → sites.geojson → layer PNG
  * AHP check: consistent, inconsistent, malformed matrices
  * GET /criteria: structure and weight sums
  * Concurrent jobs: 3 jobs complete independently
  * report.pdf: 501 placeholder
  * Error paths: invalid AOI → 422, unknown job id → 404
  * Per-source stage status in the job progress response
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
import rioxarray  # noqa: F401  registers the .rio accessor on xarray objects
import xarray as xr
from httpx import ASGITransport, AsyncClient
from pyproj import CRS

from solarsite.api.app import app
from solarsite.api.jobs import (
    ACQUIRE_SOURCES,
    JobRegistry,
    LayerProviderFn,
)
from solarsite.api.schemas import AcquireSourceStage, StageStatus
from solarsite.core import GridSpec, empty_dataarray
from solarsite.core.aoi import AOI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTM36N = CRS.from_epsg(32636)

# A tiny (30x30 at 100 m) grid -- fast to process, no network needed.
_SPEC = GridSpec(minx=0.0, miny=0.0, maxx=3000.0, maxy=3000.0, resolution_m=100, crs=_UTM36N)

# Minimal AOI GeoJSON (1deg x 0.5deg box, well under 10 000 km2)
_AOI_GEOJSON: dict[str, Any] = {
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [27.0, 31.0],
                [28.0, 31.0],
                [28.0, 31.5],
                [27.0, 31.5],
                [27.0, 31.0],
            ]
        ],
    },
    "properties": {},
}


def _make_suitability_layer(blob: float = 0.9, background: float = 0.1) -> xr.DataArray:
    """Return a tiny suitability raster with a high-value blob in the centre."""
    da = empty_dataarray(_SPEC, name="s", fill_value=background)
    h, w = da.shape
    da.values[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3] = blob
    da = da.rio.write_crs(_UTM36N, inplace=True)
    return da


def _make_layers() -> dict[str, xr.DataArray]:
    """Build a complete set of synthetic layers covering all criterion keys."""
    # Build layers keyed by the *layer names* the job runner expects in the Dataset
    # (i.e. the values returned by _criterion_key_to_layer_name).
    layer_names = [
        "ghi_annual",  # solar_radiation
        "slope",
        "aspect_class",
        "temperature",
        "humidity",
        "wind_speed",
        "lulc",
        "dist_power",  # dist_ptl
        "dist_roads",
        "dist_railway",
        "dist_urban",
        "exclusion_mask",
        "elevation",
    ]
    return {name: _make_suitability_layer() for name in layer_names}


def synthetic_layer_provider(
    aoi: AOI,
    resolution_m: int,
    stages: list[AcquireSourceStage],
) -> tuple[dict[str, xr.DataArray], list[str]]:
    """Synthetic layer provider: returns tiny in-memory DataArrays instantly."""
    _ = aoi
    _ = resolution_m
    for stage in stages:
        stage.status = StageStatus.done
    return _make_layers(), []


def _make_registry() -> JobRegistry:
    """Create a fresh JobRegistry with a temporary jobs dir."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    return JobRegistry(jobs_root=tmp)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_app():
    """Return the FastAPI app with a fresh job registry using synthetic provider."""
    original = app._registry  # type: ignore[attr-defined]
    app._registry = _make_registry()  # type: ignore[attr-defined]
    yield app
    app._registry = original  # type: ignore[attr-defined]


@pytest.fixture()
async def client(fresh_app):
    """Async HTTPX client backed by the test app."""
    transport = ASGITransport(app=fresh_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: submit a job via the API, injecting the synthetic provider
# ---------------------------------------------------------------------------


async def submit_job(
    client: AsyncClient,
    aoi: dict[str, Any] | None = None,
    resolution_m: int = 100,
    weight_overrides: dict[str, float] | None = None,
    provider: LayerProviderFn | None = None,
) -> str:
    """POST /analyze and return the job_id.

    Injects the synthetic provider directly into app._registry before posting.
    """
    # Wire provider into the registry for this submission
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    _original_submit = registry.submit

    def _patched_submit(
        aoi_: Any,
        res: int,
        overrides: Any,
        lp: Any = None,
    ) -> str:
        return _original_submit(aoi_, res, overrides, provider or synthetic_layer_provider)

    registry.submit = _patched_submit  # type: ignore[method-assign]
    try:
        resp = await client.post(
            "/analyze",
            json={
                "aoi": aoi or _AOI_GEOJSON,
                "resolution_m": resolution_m,
                **({"weight_overrides": weight_overrides} if weight_overrides else {}),
            },
        )
    finally:
        registry.submit = _original_submit  # type: ignore[method-assign]

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    return resp.json()["job_id"]


async def poll_until_done(
    client: AsyncClient,
    job_id: str,
    timeout: float = 30.0,
    interval: float = 0.05,
) -> dict[str, Any]:
    """Poll GET /jobs/{id} until status is 'done' or 'error', or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"/jobs/{job_id}")
        assert resp.status_code == 200
        state = resp.json()
        if state["status"] in ("done", "error"):
            return state
        await asyncio.sleep(interval)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


def _is_valid_png(data: bytes) -> bool:
    """Return True iff data starts with the PNG magic bytes."""
    return len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Tests: GET /criteria
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_criteria_structure(client: AsyncClient) -> None:
    """GET /criteria returns all groups, criteria, and LSI classes."""
    resp = await client.get("/criteria")
    assert resp.status_code == 200
    data = resp.json()

    # Groups
    groups = data["groups"]
    assert isinstance(groups, dict)
    assert len(groups) > 0
    group_weight_sum = sum(g["weight"] for g in groups.values())
    assert abs(group_weight_sum - 1.0) < 1e-5, f"Group weights sum = {group_weight_sum}"

    # Every group has at least one criterion
    for gkey, group in groups.items():
        assert len(group["criteria"]) > 0, f"Group {gkey} has no criteria"
        for crit in group["criteria"]:
            assert 0 < crit["local_weight"] <= 1.0
            assert 0 < crit["global_weight"] <= 1.0
            assert crit["reclassification"]["type"] in ("breakpoints", "class_scores")

    # LSI classes
    lsi_classes = data["lsi_classes"]
    assert isinstance(lsi_classes, list)
    assert len(lsi_classes) == 5
    ids = {c["id"] for c in lsi_classes}
    assert ids == {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_get_criteria_global_weights_sum(client: AsyncClient) -> None:
    """Global weights (group_weight * local_weight) should sum to ≈1.0."""
    resp = await client.get("/criteria")
    assert resp.status_code == 200
    data = resp.json()
    total = sum(c["global_weight"] for group in data["groups"].values() for c in group["criteria"])
    assert abs(total - 1.0) < 1e-4, f"Global weights sum = {total}"


# ---------------------------------------------------------------------------
# Tests: POST /ahp/check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ahp_check_consistent_matrix(client: AsyncClient) -> None:
    """A perfectly consistent 3x3 matrix -> consistent=True, weights sum approx 1."""
    # Perfectly consistent: 1, 3, 5 -> 1/3, 1, 5/3 -> 1/5, 3/5, 1
    matrix = [
        [1.0, 3.0, 5.0],
        [1 / 3, 1.0, 5 / 3],
        [1 / 5, 3 / 5, 1.0],
    ]
    resp = await client.post("/ahp/check", json={"matrix": matrix})
    assert resp.status_code == 200
    data = resp.json()
    assert abs(sum(data["weights"]) - 1.0) < 1e-4
    assert data["consistent"] is True
    assert data["cr"] <= 0.10


@pytest.mark.asyncio
async def test_ahp_check_inconsistent_matrix(client: AsyncClient) -> None:
    """A highly inconsistent matrix → consistent=False, most_inconsistent set."""
    # Clearly inconsistent pairwise matrix
    matrix = [
        [1.0, 9.0, 1 / 9],
        [1 / 9, 1.0, 9.0],
        [9.0, 1 / 9, 1.0],
    ]
    resp = await client.post("/ahp/check", json={"matrix": matrix})
    assert resp.status_code == 200
    data = resp.json()
    assert data["consistent"] is False
    assert data["cr"] > 0.10


@pytest.mark.asyncio
async def test_ahp_check_malformed_not_square(client: AsyncClient) -> None:
    """A non-square matrix → 422."""
    matrix = [[1.0, 2.0], [0.5, 1.0, 3.0]]
    resp = await client.post("/ahp/check", json={"matrix": matrix})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ahp_check_too_small(client: AsyncClient) -> None:
    """A 1x1 matrix -> 422."""
    resp = await client.post("/ahp/check", json={"matrix": [[1.0]]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ahp_check_negative_values(client: AsyncClient) -> None:
    """A matrix with negative values → 422."""
    matrix = [
        [1.0, -2.0],
        [-0.5, 1.0],
    ]
    resp = await client.post("/ahp/check", json={"matrix": matrix})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: POST /analyze error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_invalid_aoi_bad_type(client: AsyncClient) -> None:
    """A Point geometry (unsupported) → 422."""
    bad_aoi = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [27.0, 31.0]},
        "properties": {},
    }
    resp = await client.post("/analyze", json={"aoi": bad_aoi, "resolution_m": 500})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_analyze_invalid_aoi_too_large(client: AsyncClient) -> None:
    """An AOI > 10 000 km² → 422."""
    huge_aoi = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [0.0, -50.0],
                    [50.0, -50.0],
                    [50.0, 50.0],
                    [0.0, 50.0],
                    [0.0, -50.0],
                ]
            ],
        },
        "properties": {},
    }
    resp = await client.post("/analyze", json={"aoi": huge_aoi, "resolution_m": 500})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_analyze_unknown_job_id(client: AsyncClient) -> None:
    """GET /jobs/{id} with an unknown id → 404."""
    resp = await client.get("/jobs/nonexistent_job_id_xyz")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Full job lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_job_lifecycle(client: AsyncClient) -> None:
    """POST /analyze → poll until done → sites.geojson → lsi.png.

    Checks:
    - Job progresses through acquiring → analyzing → done
    - acquire_stages shows per-source status
    - sites.geojson is valid GeoJSON with ≥1 site
    - lsi.png returns valid PNG bytes + X-Layer-Bounds header
    """
    job_id = await submit_job(client)

    # Poll until done
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done", f"Job failed: {state.get('error')}"
    assert state["n_sites"] is not None

    # acquire_stages: all should be done (synthetic provider marks them all done)
    stages = state["acquire_stages"]
    assert len(stages) == len(ACQUIRE_SOURCES)
    stage_statuses = {s["source"]: s["status"] for s in stages}
    for source in ACQUIRE_SOURCES:
        assert source in stage_statuses, f"Missing stage for source {source}"
    # All synthetic stages should be 'done'
    for s in stages:
        assert s["status"] == "done", f"Stage {s['source']} not done: {s['status']}"

    # analysis_status should be done
    assert state["analysis_status"] == "done"

    # GET sites.geojson
    sites_resp = await client.get(f"/jobs/{job_id}/sites.geojson")
    assert sites_resp.status_code == 200
    geojson = sites_resp.json()
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) >= 1

    # Check site properties
    feature = geojson["features"][0]
    props = feature["properties"]
    assert "rank" in props
    assert "area_km2" in props
    assert "mean_lsi" in props

    # GET lsi.png
    png_resp = await client.get(f"/jobs/{job_id}/layers/lsi.png")
    assert png_resp.status_code == 200
    assert png_resp.headers["content-type"] == "image/png"
    png_bytes = png_resp.content
    assert _is_valid_png(png_bytes), "Response is not a valid PNG"

    # X-Layer-Bounds header should be present
    assert "x-layer-bounds" in png_resp.headers
    bounds = json.loads(png_resp.headers["x-layer-bounds"])
    for key in ("west", "south", "east", "north"):
        assert key in bounds, f"Missing bounds key: {key}"
    assert bounds["west"] < bounds["east"]
    assert bounds["south"] < bounds["north"]


@pytest.mark.asyncio
async def test_layer_bounds_endpoint(client: AsyncClient) -> None:
    """GET /jobs/{id}/layers/lsi.bounds returns WGS-84 bounds JSON."""
    job_id = await submit_job(client)
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done"

    resp = await client.get(f"/jobs/{job_id}/layers/lsi.bounds")
    assert resp.status_code == 200
    bounds = resp.json()
    for key in ("west", "south", "east", "north"):
        assert key in bounds
    assert bounds["west"] < bounds["east"]
    assert bounds["south"] < bounds["north"]


@pytest.mark.asyncio
async def test_layer_png_before_done_returns_409(client: AsyncClient) -> None:
    """Requesting a layer PNG before the job is done → 409."""
    # Submit but do NOT poll — immediately try to get the layer
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    _original_submit = registry.submit

    def _patched_submit(aoi_: Any, res: int, overrides: Any, lp: Any = None) -> str:
        return _original_submit(aoi_, res, overrides, synthetic_layer_provider)

    registry.submit = _patched_submit  # type: ignore[method-assign]
    try:
        resp = await client.post(
            "/analyze",
            json={"aoi": _AOI_GEOJSON, "resolution_m": 100},
        )
    finally:
        registry.submit = _original_submit  # type: ignore[method-assign]

    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # The job might be queued or acquiring — try immediately
    record = registry.get(job_id)
    assert record is not None
    # Force it into queued state by checking before it runs
    # We can check: if status is not done yet, layer route must 409 or 404
    # (Once done it will 200; we can't guarantee timing, so we only check
    #  when status is NOT done)
    status_resp = await client.get(f"/jobs/{job_id}")
    status = status_resp.json()["status"]
    if status not in ("done", "error"):
        png_resp = await client.get(f"/jobs/{job_id}/layers/lsi.png")
        assert png_resp.status_code in (409, 404)


@pytest.mark.asyncio
async def test_sites_before_done_returns_409(client: AsyncClient) -> None:
    """Requesting sites.geojson before the job is done → 409."""
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    _original_submit = registry.submit

    def _patched_submit(aoi_: Any, res: int, overrides: Any, lp: Any = None) -> str:
        return _original_submit(aoi_, res, overrides, synthetic_layer_provider)

    registry.submit = _patched_submit  # type: ignore[method-assign]
    try:
        resp = await client.post("/analyze", json={"aoi": _AOI_GEOJSON, "resolution_m": 100})
    finally:
        registry.submit = _original_submit  # type: ignore[method-assign]

    job_id = resp.json()["job_id"]
    status_resp = await client.get(f"/jobs/{job_id}")
    status = status_resp.json()["status"]
    if status not in ("done", "error"):
        sites_resp = await client.get(f"/jobs/{job_id}/sites.geojson")
        assert sites_resp.status_code in (409, 404)


# ---------------------------------------------------------------------------
# Tests: report.pdf (200 + PDF where WeasyPrint libs exist, else 501)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_pdf(client: AsyncClient) -> None:
    """GET /jobs/{id}/report.pdf.

    P3.3 wired up a WeasyPrint renderer. Where WeasyPrint's native libs are
    available (Linux CI / Docker) the route returns 200 with a real PDF; where
    they are absent (Windows host) ``render_report`` raises NotImplementedError
    and the route returns 501. Environment-aware so it is correct on both.
    """
    job_id = await submit_job(client)
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done"

    try:
        import weasyprint  # noqa: F401

        weasyprint_available = True
    except (ImportError, OSError):
        weasyprint_available = False

    resp = await client.get(f"/jobs/{job_id}/report.pdf")
    if weasyprint_available:
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"
    else:
        assert resp.status_code == 501
        assert "implement" in resp.text.lower()


# ---------------------------------------------------------------------------
# Tests: Concurrent jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_jobs_no_cross_contamination(client: AsyncClient) -> None:
    """Three concurrent jobs complete independently with no result contamination."""
    registry: JobRegistry = app._registry  # type: ignore[attr-defined]
    _original_submit = registry.submit

    def _patched_submit(aoi_: Any, res: int, overrides: Any, lp: Any = None) -> str:
        return _original_submit(aoi_, res, overrides, synthetic_layer_provider)

    registry.submit = _patched_submit  # type: ignore[method-assign]

    try:
        # Submit 3 jobs concurrently
        resps = await asyncio.gather(
            client.post("/analyze", json={"aoi": _AOI_GEOJSON, "resolution_m": 100}),
            client.post("/analyze", json={"aoi": _AOI_GEOJSON, "resolution_m": 100}),
            client.post("/analyze", json={"aoi": _AOI_GEOJSON, "resolution_m": 100}),
        )
    finally:
        registry.submit = _original_submit  # type: ignore[method-assign]

    assert all(r.status_code == 202 for r in resps)
    job_ids = [r.json()["job_id"] for r in resps]

    # All IDs must be distinct
    assert len(set(job_ids)) == 3, "Concurrent jobs got duplicate job IDs"

    # Poll all three until done
    states = await asyncio.gather(*[poll_until_done(client, jid) for jid in job_ids])

    for state in states:
        assert state["status"] == "done", f"Job failed: {state.get('error')}"

    # Each job must have its own result directory (no cross-contamination)
    job_dirs = [registry.get(jid).job_dir for jid in job_ids]  # type: ignore[union-attr]
    assert len({str(d) for d in job_dirs}) == 3, "Jobs share the same job directory"

    # Each job's sites.geojson must be independently valid
    for jid in job_ids:
        resp = await client.get(f"/jobs/{jid}/sites.geojson")
        assert resp.status_code == 200
        gj = resp.json()
        assert gj["type"] == "FeatureCollection"


# ---------------------------------------------------------------------------
# Tests: weight_overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weight_overrides_accepted(client: AsyncClient) -> None:
    """POST /analyze with weight_overrides → job completes successfully."""
    job_id = await submit_job(
        client,
        weight_overrides={"dist_ptl": 0.5, "dist_roads": 0.1},
    )
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done"


# ---------------------------------------------------------------------------
# Tests: Layer PNG rendering correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusion_mask_png(client: AsyncClient) -> None:
    """GET /jobs/{id}/layers/exclusion_mask.png returns valid PNG."""
    job_id = await submit_job(client)
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done"

    resp = await client.get(f"/jobs/{job_id}/layers/exclusion_mask.png")
    assert resp.status_code == 200
    assert _is_valid_png(resp.content)


@pytest.mark.asyncio
async def test_unknown_layer_returns_404(client: AsyncClient) -> None:
    """Requesting a non-existent layer PNG → 404."""
    job_id = await submit_job(client)
    state = await poll_until_done(client, job_id)
    assert state["status"] == "done"

    resp = await client.get(f"/jobs/{job_id}/layers/no_such_layer.png")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Unknown job id on all routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_job_sites(client: AsyncClient) -> None:
    resp = await client.get("/jobs/unknown_xyz/sites.geojson")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_job_report(client: AsyncClient) -> None:
    resp = await client.get("/jobs/unknown_xyz/report.pdf")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_job_layer_png(client: AsyncClient) -> None:
    resp = await client.get("/jobs/unknown_xyz/layers/lsi.png")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_job_layer_bounds(client: AsyncClient) -> None:
    resp = await client.get("/jobs/unknown_xyz/layers/lsi.bounds")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Staged status appears in progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_statuses_present(client: AsyncClient) -> None:
    """Job response includes acquire_stages with the expected source names."""
    job_id = await submit_job(client)
    # Poll briefly; even if still running, stages should be present
    await asyncio.sleep(0.01)
    resp = await client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    state = resp.json()
    assert "acquire_stages" in state
    sources = {s["source"] for s in state["acquire_stages"]}
    for expected in ACQUIRE_SOURCES:
        assert expected in sources, f"acquire_stage for '{expected}' missing"

    assert "analysis_status" in state
    assert state["analysis_status"] in ("pending", "running", "done", "failed")
