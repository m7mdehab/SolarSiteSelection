# MASTER PLAN — Project B: Solar Site Selection (Geospatial PV Suitability Engine)

**Issued by:** PM (Claude, claude.ai session)
**Addressed to:** Master Agent (Claude Code, running in the `SolarSiteSelection` repo)
**Mission:** The repo is currently empty (blank `app.py`); the legacy product was a Flutter APK questionnaire that asked users to supply their own geodata. Replace it entirely with a web application where the user **draws an area on a map and the system does everything else**: fetches all geodata from public APIs, runs a live, consistency-checked AHP multi-criteria analysis, produces a Land Suitability Index raster, extracts ranked candidate sites, estimates energy yield with pvlib, and exports a PDF report. Web only. No mobile.

---

## 0. ORCHESTRATION PROTOCOL

Identical governance to Project A — restated here so this document is self-sufficient:

1. Parse this plan into a task graph. Phases gate sequentially; subphases within a phase run as **parallel subagents** unless `depends:` says otherwise.
2. Spawn subagents with: the subphase spec verbatim, acceptance criteria, repo conventions (§0.4), and the instruction to report back work done, test results, open questions, and plan contradictions discovered.
3. Review all work yourself as a hostile senior reviewer. Run the acceptance commands. Reject with specific corrections; re-dispatch; recurse until pass. Never weaken a gate — impossible gates get a `docs/DEVIATIONS.md` entry plus the prescribed fallback.
4. Phase Gate commands must pass before the next phase opens.
5. Never block on the human outside the Registry (§0.2). Missing registry item → mark `BLOCKED(HUMAN:<id>)` in `docs/STATUS.md`, build around it.
6. Maintain `docs/STATUS.md` (live board) and `docs/DECISIONS.md` (one-line decisions + rationale).
7. Deliver the §6 final report. The PM re-runs your gates independently.

**Concurrency:** directory-ownership partitioning per subphase; shared files (`pyproject.toml`, CI) edited only by Master Agent. Commits prefixed `[P<phase>.<sub>]`; `make check` (ruff + pyright + fast pytest) green before every commit. **No binaries or downloaded datasets in git** — download scripts with checksums and an aggressive cache directory instead.

### 0.2 HUMAN INPUT REGISTRY (everything the human must do, collected up front)
| ID | Item | Needed by | Fallback if absent |
|---|---|---|---|
| H1 | OpenTopography API key in `.env` as `OPENTOPO_KEY` (free, instant signup) | P1.2 | Use Copernicus GLO-30 DEM from the public AWS bucket (no key) as primary instead; OpenTopography becomes optional secondary. Attempt the no-key path FIRST so this item may self-resolve. |
| H2 | WDPA protected-areas file: human downloads the Egypt WDPA geopackage from protectedplanet.net (manual license click-through) into `data/manual/wdpa_egypt.gpkg` | P1.4 | Protected-areas layer marked "unavailable" in outputs with a visible caveat; analysis runs without it. |
| H3 | Hugging Face token (`HF_TOKEN` in `.env`) for Spaces deployment | P4.4 | Build + document `make deploy`; do not execute. |
| H4 | Go/no-go for public deployment in `docs/HUMAN_APPROVALS.md` | P4.4 | Same as H3. |

Note how short this list is — that is by design. PVGIS, Overpass/OSM, ESA WorldCover (AWS public bucket), Copernicus DEM (AWS public bucket), and Natural Earth all require **no credentials**. Build against those first.

### 0.3 Stack decisions (made by PM — do not relitigate)
Python 3.11, `uv` + `pyproject.toml`. Geospatial core: rasterio, rioxarray, xarray, geopandas, shapely, pyproj. Terrain: `xarray`-native slope/aspect (or richdem if it installs cleanly — decide once). Energy: **pvlib** (non-negotiable; it is the domain-fluency signal). Backend: FastAPI with an in-process async job queue (no Redis/Celery). Frontend: React + Vite + MapLibre GL + Mapbox-draw-compatible drawing plugin. Raster tiles to the browser: render LSI to colormapped PNG overlays with bounds (simple, robust) — not a tile server. PDF reports: WeasyPrint from an HTML template. Tests: pytest with recorded HTTP fixtures (`responses`/`respx`) — CI must never hit live APIs. Lint/type: ruff + pyright. Container: single Dockerfile, port 7860, HF Spaces Docker SDK.

### 0.4 Repo conventions
```
SolarSiteSelection/
  src/solarsite/
    acquire/      # one module per data source, common AOI-in → raster/vector-out interface
    analysis/     # ahp.py, reclassify.py, overlay.py, sites.py, energy.py
    api/          # FastAPI app, job manager, report renderer
  web/            # React frontend
  configs/        # criteria.yaml (the AHP criteria registry — single source of truth)
  scripts/        # validate_against_paper.py, demo_aoi.py
  tests/          # unit + fixture-based integration
  docs/           # STATUS, DECISIONS, DEVIATIONS, ARCHITECTURE, methodology.md, validation/
  data/           # gitignored cache + manual/ for H2
  Dockerfile  Makefile  pyproject.toml  .github/workflows/ci.yml
```
Everything typed. The acquisition layer's contract: `fetch(aoi: Polygon, resolution_m: int) -> xr.DataArray | gpd.GeoDataFrame`, always returned in a common equal-area working CRS chosen in P0.2, always cached on disk keyed by (source, AOI hash, params).

---

## PHASE 0 — RESET & FOUNDATION

**P0.1 — Repo reset** *(owns: root)*
Tag current state `v0-empty` for the record. Delete the blank `app.py` and stale requirements. Scaffold §0.4, MIT LICENSE, `.gitignore`, `.env.example`, honest placeholder README ("rebuild in progress — see docs/STATUS.md").
*Accept:* fresh clone → `uv sync && make check` green (with skeleton tests).

**P0.2 — Geospatial kernel** *(owns: src/solarsite/core/)*
The shared plumbing every other subagent will import: AOI model (GeoJSON polygon in, validated, area-limited to ≤ 10,000 km² with a clear error), working-CRS policy (pick the appropriate equal-area/UTM strategy for Egypt-and-general use; document in DECISIONS), raster grid spec (default 100 m resolution, configurable), reproject/align/resample utilities, the disk cache, and proximity-raster computation (Euclidean distance from vector features → raster). Heavily unit-tested — this is the layer where silent geospatial bugs are born.
*Accept:* property-style tests: round-trip reprojection error < 1 cell; distance raster validated against hand-computed fixture; cache hit/miss behavior tested.

**P0.3 — Criteria registry** *(owns: configs/criteria.yaml, src/solarsite/analysis/registry.py)*
Encode the full criteria tree from the reference methodology (Habib et al. 2020, which the legacy project was built on): three groups (Economic 50%, Technical 25%, Environmental 25%) and their sub-criteria — solar radiation, slope, distances to PTL/roads/railway/urban, shadow, aspect, humidity, wind speed, temperature, land use/cover, land capability — each with its published default weight, its reclassification breakpoints (suitable-range tables from the paper), data source binding, and whether it is a factor or a hard-exclusion constraint. This YAML is the single source of truth the AHP module, overlay engine, frontend, and PDF report all read.
*Accept:* schema-validated; default weights sum to 1 within tolerance; loader tested.

**PHASE 0 GATE:** all accepts; CI live; STATUS/DECISIONS current.

---

## PHASE 1 — DATA ACQUISITION LAYER (max parallelism: five independent subagents)

Each subphase owns exactly one module in `acquire/`, implements the §0.4 contract, includes recorded-fixture tests, graceful retry/backoff, and a `--live` smoke script the Master Agent runs once manually to verify reality matches the fixtures.

**P1.1 — Solar resource (PVGIS).** Monthly/annual GHI + the TMY endpoint for later pvlib use. No key needed. Interpolate point queries to the AOI grid (PVGIS is point-based: implement a sampling grid + interpolation, document density choice).
**P1.2 — Terrain (Copernicus GLO-30 / OpenTopography).** DEM mosaic for AOI → elevation, slope (degrees), aspect (8-way classes per the criteria table). `depends: H1 only for the OpenTopography variant`.
**P1.3 — Infrastructure (OSM Overpass).** Power lines (`power=line`), substations, roads (motorway→secondary), railways, urban areas (landuse=residential + place polygons) → GeoDataFrames → proximity rasters via the P0.2 kernel. Respect Overpass rate limits; chunk large AOIs.
**P1.4 — Land cover & exclusions (ESA WorldCover + WDPA).** WorldCover 10 m from the public AWS bucket, resampled to grid, mapped to the criteria table's LULC suitability classes; water bodies and built-up as hard exclusions. WDPA from `data/manual/` (H2) as hard exclusion.
**P1.5 — Climate (Open-Meteo archive API).** Annual mean temperature, RH, wind speed rasters via grid sampling + interpolation (no key). Wind speed is also surfaced as a "bonus hybrid-potential" layer, mirroring the legacy app's inclusion of it.

**PHASE 1 GATE:** `python scripts/demo_aoi.py --aoi tests/fixtures/nw_coast_aoi.geojson --offline` produces an aligned multi-layer xarray Dataset from cached fixtures; every acquire module ≥ 85% coverage; one `--live` verification per source logged in STATUS (or BLOCKED with reason).

---

## PHASE 2 — ANALYSIS ENGINE (the intellectual core)

**P2.1 — AHP module** *(owns: analysis/ahp.py)*
Full Saaty implementation: pairwise matrix → principal eigenvector weights (power iteration), λmax, CI, CR with the standard RI table, **rejection of CR > 0.10** with a structured error identifying the most inconsistent judgment triple (so the UI can highlight it). Must reproduce the paper's published weight vectors from its published pairwise matrices within 1% — this is a unit test, and it is the project's mathematical credibility anchor.
*Accept:* paper-reproduction test green; CR boundary tests; property test (consistent matrices → CR ≈ 0).

**P2.2 — Reclassification & weighted overlay** *(owns: analysis/reclassify.py, overlay.py)* `depends: P0.3`
Apply the criteria.yaml breakpoint tables to each layer → 0–1 (or class-score) suitability rasters; apply hard exclusions as a binary mask; weighted overlay → continuous LSI; quantile/threshold reclassification into the five literature classes (most → restricted). All pure xarray ops, fixture-tested with tiny synthetic rasters where the answer is hand-computable.
**P2.3 — Site extraction** *(owns: analysis/sites.py)* `depends: P2.2`
From the LSI: contiguous regions of top-class cells (connected components), minimum-area filter (configurable, default 0.5 km² — enough for a utility-scale plant), polygonization, ranking by (mean LSI, area, distance-to-PTL), top-k candidates with stats per site.
**P2.4 — Energy & economics** *(owns: analysis/energy.py)* `depends: P1.1`
Per candidate site: pvlib ModelChain with PVGIS TMY for a representative fixed-tilt system (sane defaults: tilt ≈ latitude, 0.96 DC/AC, documented loss assumptions) → specific yield (kWh/kWp/yr) and total annual GWh at a configurable packing density (MWp/km²); simple LCOE from configurable CAPEX/OPEX defaults with all assumptions surfaced in the report. Validate specific yield for the NW-coast fixture against PVGIS's own PVcalc output within 5%.
**P2.5 — Validation against the reference study** *(owns: scripts/validate_against_paper.py, docs/validation/)* `depends: P2.2`
Run the full engine over the Habib et al. NW-coast study area with the paper's weights; compare the resulting suitability-class distribution and spatial pattern against the paper's published LSI map (figure-level comparison + class-percentage table). Perfect agreement is impossible (different data vintages); the deliverable is an honest quantified comparison and a discussion of divergences. This document is the README's centerpiece.

**PHASE 2 GATE:** synthetic end-to-end (fixtures → LSI → sites → energy) test green in CI; AHP paper-reproduction green; validation doc written with real numbers.

---

## PHASE 3 — WEB APPLICATION

**P3.1 — API & job system** *(owns: src/solarsite/api/)* `depends: P2.x interfaces (can start against stubs)`
FastAPI: `POST /analyze` (AOI GeoJSON + optional weight overrides + resolution → job id), `GET /jobs/{id}` (staged progress: acquiring[source]→analyzing→done, with per-stage status so the UI can show a real pipeline progress view, not a fake bar), `GET /jobs/{id}/layers/{name}.png` (colormapped raster overlays + bounds metadata), `GET /jobs/{id}/sites.geojson`, `GET /jobs/{id}/report.pdf`, `POST /ahp/check` (pairwise matrix → weights + CR, for live UI feedback), `GET /criteria` (the registry, for UI rendering). In-process queue, results cached on disk.
*Accept:* httpx test suite covering the full job lifecycle against fixture data; concurrent-jobs test.

**P3.2 — Frontend** *(owns: web/)*
React + MapLibre. Flow: (1) draw/upload AOI (with area-limit feedback); (2) **criteria panel** auto-rendered from `/criteria` — default paper weights shown, expert mode opens the pairwise-comparison editor with **live CR gauge** that turns red and pinpoints the inconsistent judgments via `/ahp/check`; (3) run → staged progress; (4) results: toggleable map layers (each input criterion, exclusion mask, final LSI with the 5-class legend), candidate-site polygons with click-popups (rank, area, mean LSI, kWh/kWp/yr, est. GWh/yr, LCOE), side panel ranking table; (5) downloads: GeoTIFF (LSI), GeoJSON (sites), PDF report. Ship 2–3 preset demo AOIs (NW coast Egypt, one generic desert AOI) so a visitor gets a full result without drawing anything. Restrained professional design per frontend-design conventions.
*Accept:* Playwright: load → pick preset AOI → run (against a seeded fixture job) → LSI layer renders → click a site → popup shows yield.

**P3.3 — PDF report** *(owns: api/report/)* `depends: P3.1`
WeasyPrint from an HTML template: AOI map snapshot, methodology summary (auto-generated from criteria.yaml — weights actually used, CR achieved), per-criterion thumbnails, LSI map, candidate-site table with energy/economics, assumptions appendix, data-source citations with retrieval dates. This is the legacy app's one good feature, rebuilt properly.
*Accept:* golden-file test (structure, not pixels); renders in < 30 s.

**P3.4 — Container** *(owns: Dockerfile)* `depends: P3.1–P3.3`
Multi-stage (web build → python runtime, GDAL stack via wheels not apt-compiled where possible). `docker compose up` → :7860.
*Accept:* CI builds; healthcheck; image < 3 GB.

**PHASE 3 GATE:** fresh clone → `docker compose up` → preset-AOI offline demo completes in-browser; Playwright green.

---

## PHASE 4 — HARDENING, DOCS, DEPLOY

**P4.1 — Tests/CI completion:** ≥ 80% coverage on `src/solarsite` (acquire fixtures + analysis math + API); live-API tests marked and excluded from CI; lint/type/test/docker matrix; badges.
**P4.2 — Documentation:** README landing page — the one-sentence pitch ("draw an area, get a defensible PV siting analysis"), demo GIF, architecture diagram (mermaid: acquire → analyze → serve), the validation-vs-paper section with its table, quickstart verified by a CI job, limitations (data vintages, point-interpolated climate layers, AHP subjectivity, LCOE simplifications). `docs/methodology.md` translating the criteria.yaml into prose with citations.
**P4.3 — Legacy reconciliation:** archive the APK story in `docs/history.md` (one honest paragraph: what the v1 Flutter app did, why it was replaced); ensure no doc anywhere still asks users to input their own geodata.
**P4.4 — Deploy** `depends: H3, H4`: HF Space (Docker), demo AOIs work in production with the offline cache pre-seeded so the public demo never depends on third-party API uptime, link in README.

**PHASE 4 GATE = PROJECT GATE:** all prior gates re-run from fresh clone; STATUS zero open non-BLOCKED items; final report written.

---

## 6. FINAL REPORT SPEC
`docs/FINAL_REPORT.md`: (1) executive summary; (2) phase table — subphase, subagent dispatches, review iterations, final state; (3) validation-vs-paper results verbatim; (4) the demo walkthrough with screenshots; (5) all DEVIATIONS with rationale; (6) all BLOCKED(HUMAN) items with the exact single action required; (7) verification transcript — literal output of every phase-gate command run top-to-bottom on final state; (8) next-week recommendations. The PM independently re-runs the gates; transcript-vs-reality divergence is the single disqualifying failure.
