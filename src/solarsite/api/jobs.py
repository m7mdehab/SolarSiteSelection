"""In-process async job manager for the SolarSiteSelection analysis pipeline.

Design
------
* ``JobRegistry`` holds a dict of ``_JobRecord`` (in-memory; survives the
  process, not a restart).  Results are also persisted to ``data/jobs/{id}/``
  so the layer PNG / sites GeoJSON / report PDF routes can stream files.

* Each job runs as an ``asyncio.Task`` (via ``asyncio.create_task``).  The
  task calls ``_run_pipeline`` which:
    1. Iterates over ACQUIRE_SOURCES (ordered), updating per-source stage status.
    2. Calls the *layer provider* — an injected callable — for each source.
    3. Runs the analysis stages (reclassify → weighted_overlay → classify_lsi →
       extract_sites → site_energy) in the same task (CPU-bound but small enough
       at test resolution to be acceptable; a threadpool could be added later).
    4. Persists artefacts (layers as NetCDF + PNG, sites as GeoJSON).

Injectable layer provider
-------------------------
The ``layer_provider`` argument to ``JobRegistry.submit`` (and defaulting to
``default_layer_provider``) is the seam that makes tests fast and offline.

Interface::

    LayerProviderFn = Callable[
        [AOI, int, list[AcquireSourceStage]],
        tuple[dict[str, xr.DataArray], list[str]],
    ]

The callable receives:
  * ``aoi``          — validated AOI
  * ``resolution_m`` — grid resolution
  * ``stages``       — mutable list of AcquireSourceStage; the provider MUST
                        update each entry's status as it goes (running → done /
                        failed).  This drives the real-time progress UI.

It returns:
  * A dict mapping layer name → xr.DataArray (already aligned to a common grid)
  * A list of skipped source labels (for the job state ``skipped_sources`` field)

``default_layer_provider`` wraps ``build_dataset`` from ``scripts/demo_aoi.py``
and translates its progress into stage updates.

Report renderer interface (for P3.3)
-------------------------------------
``render_report`` is imported from ``solarsite.api.render``.  P3.3 can replace
the stub with a WeasyPrint implementation.  The function signature is::

    def render_report(job_id: str, job_dir: Path) -> bytes:
        ...

It must return the PDF bytes (or raise ``NotImplementedError`` to get a 501).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib as mpl
import numpy as np
import rioxarray  # noqa: F401  registers the .rio accessor on xarray objects
import xarray as xr
from pyproj import CRS, Transformer

# Force non-interactive backend before any pyplot import.
# This call is safe here because pyplot hasn't been imported yet.
mpl.use("Agg")

from solarsite.analysis import (
    classify_lsi,
    extract_sites,
    load_registry,
    reclassify_layer,
    weighted_overlay,
)
from solarsite.api.schemas import (
    AcquireSourceStage,
    JobState,
    JobStatus,
    StageStatus,
)
from solarsite.core import AOI

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type alias for the injectable layer provider
# ---------------------------------------------------------------------------

#: Callable[[AOI, int, list[AcquireSourceStage]], tuple[dict[str, xr.DataArray], list[str]]]
LayerProviderFn = Callable[
    [AOI, int, list[AcquireSourceStage]],
    tuple[dict[str, xr.DataArray], list[str]],
]

# ---------------------------------------------------------------------------
# Ordered acquire-source labels (used to pre-populate stage list)
# ---------------------------------------------------------------------------

ACQUIRE_SOURCES: list[str] = [
    "solar",
    "terrain",
    "climate",
    "lulc",
    "exclusion_mask",
    "dist_power",
    "dist_roads",
    "dist_railway",
    "dist_urban",
]

# ---------------------------------------------------------------------------
# Default jobs root
# ---------------------------------------------------------------------------

_DEFAULT_JOBS_ROOT = Path("data/jobs")


# ---------------------------------------------------------------------------
# Default layer provider (uses real acquire sources via build_dataset)
# ---------------------------------------------------------------------------


def default_layer_provider(
    aoi: AOI,
    resolution_m: int,
    stages: list[AcquireSourceStage],
) -> tuple[dict[str, xr.DataArray], list[str]]:
    """Real acquire layer provider: wraps ``build_dataset`` from demo_aoi.

    Stage-status updates are approximated: all stages set to "running" before
    the call, then updated to "done" or "failed" based on the skipped list.
    (build_dataset doesn't expose per-source callbacks, so this is best-effort.)
    """
    from scripts.demo_aoi import build_dataset  # type: ignore[import]

    from solarsite.core import DiskCache

    # Mark all stages running
    for s in stages:
        s.status = StageStatus.running

    try:
        ds, skipped = build_dataset(aoi, resolution_m, DiskCache(), offline=False)
    except Exception as exc:
        for s in stages:
            s.status = StageStatus.failed
            s.error = str(exc)
        raise

    skipped_set = set(skipped)
    for s in stages:
        if s.source in skipped_set:
            s.status = StageStatus.failed
            s.error = "skipped by build_dataset"
        else:
            s.status = StageStatus.done

    layers: dict[str, xr.DataArray] = {str(name): ds[name] for name in ds.data_vars}
    return layers, skipped


# ---------------------------------------------------------------------------
# Internal job record
# ---------------------------------------------------------------------------


class _JobRecord:
    """Mutable state for a single analysis job."""

    def __init__(
        self,
        job_id: str,
        aoi: AOI,
        resolution_m: int,
        weight_overrides: dict[str, float] | None,
        jobs_root: Path,
    ) -> None:
        self.job_id = job_id
        self.aoi = aoi
        self.resolution_m = resolution_m
        self.weight_overrides = weight_overrides
        self.job_dir = jobs_root / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)

        self.status: JobStatus = JobStatus.queued
        self.error: str | None = None
        self.n_sites: int | None = None
        self.skipped_sources: list[str] = []
        self.analysis_status: StageStatus = StageStatus.pending
        self.analysis_error: str | None = None

        # Pre-populate acquire stages (will be updated in-place during run)
        self.acquire_stages: list[AcquireSourceStage] = [
            AcquireSourceStage(source=src, status=StageStatus.pending) for src in ACQUIRE_SOURCES
        ]

    def to_job_state(self) -> JobState:
        return JobState(
            job_id=self.job_id,
            status=self.status,
            resolution_m=self.resolution_m,
            acquire_stages=list(self.acquire_stages),
            analysis_status=self.analysis_status,
            analysis_error=self.analysis_error,
            error=self.error,
            n_sites=self.n_sites,
            skipped_sources=list(self.skipped_sources),
        )


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


async def _run_pipeline(
    record: _JobRecord,
    layer_provider: LayerProviderFn,
) -> None:
    """Execute the full acquisition + analysis pipeline for one job."""
    log.info("Job %s: starting pipeline", record.job_id)

    # ---- ACQUIRE -----------------------------------------------------------
    record.status = JobStatus.acquiring
    try:
        # Run blocking IO in threadpool so we don't block the event loop
        layers, skipped = await asyncio.get_event_loop().run_in_executor(
            None,
            layer_provider,
            record.aoi,
            record.resolution_m,
            record.acquire_stages,
        )
        record.skipped_sources = skipped
    except Exception as exc:
        record.status = JobStatus.error
        record.error = f"Acquisition failed: {exc}"
        log.exception("Job %s: acquisition failed", record.job_id)
        return

    # ---- ANALYZE -----------------------------------------------------------
    record.status = JobStatus.analyzing
    record.analysis_status = StageStatus.running
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            _run_analysis,
            record,
            layers,
        )
    except Exception as exc:
        record.status = JobStatus.error
        record.analysis_status = StageStatus.failed
        record.analysis_error = str(exc)
        record.error = f"Analysis failed: {exc}"
        log.exception("Job %s: analysis failed", record.job_id)
        return

    record.analysis_status = StageStatus.done
    record.status = JobStatus.done
    log.info("Job %s: done  n_sites=%s", record.job_id, record.n_sites)


def _apply_weight_overrides(registry: Any, overrides: dict[str, float]) -> Any:
    """Return a copy of the registry with adjusted local weights.

    Overrides are interpreted as *desired ratios* within each group: we
    scale the existing local_weight by the multiplier needed so the
    override key matches the requested weight, then renormalise the group
    so local weights still sum to 1.

    This is the simplest correct approach: replace the local_weight directly
    with the override value, then renormalise the other criteria in the group
    proportionally.
    """
    import copy

    reg = copy.deepcopy(registry)
    for key, new_weight in overrides.items():
        try:
            crit = reg.criterion(key)
        except KeyError:
            log.warning("weight_override: unknown criterion key '%s', ignoring", key)
            continue
        group = reg.groups[crit.group]
        # Find this criterion in the group's criteria list and update it
        for c in group.criteria:
            if c.key == key and c.kind == "factor":
                old = c.local_weight
                c.local_weight = float(new_weight)
                delta = float(new_weight) - old
                # Renormalise others proportionally
                others = [o for o in group.criteria if o.key != key and o.kind == "factor"]
                total_others = sum(o.local_weight for o in others)
                if total_others > 1e-9:
                    for o in others:
                        o.local_weight = max(
                            0.0, o.local_weight - delta * o.local_weight / total_others
                        )
                break
    return reg


def _run_analysis(record: _JobRecord, layers: dict[str, xr.DataArray]) -> None:
    """Synchronous analysis: reclassify → overlay → classify → sites → energy.

    Saves artefacts to ``record.job_dir``.
    """
    registry = load_registry()

    if record.weight_overrides:
        registry = _apply_weight_overrides(registry, record.weight_overrides)

    # --- reclassify each factor layer that is available ---------------------
    suitability: dict[str, xr.DataArray] = {}
    for criterion in registry.factors:
        key = criterion.key
        # Map criterion key to the layer name used in the dataset
        layer_name = _criterion_key_to_layer_name(key)
        if layer_name in layers:
            da = layers[layer_name]
            try:
                suit = reclassify_layer(da, criterion, data_unit=criterion.unit or None)
                suitability[key] = suit
            except Exception as exc:
                log.warning("Skipping reclassification for %s: %s", key, exc)

    if not suitability:
        raise RuntimeError("No suitability layers could be reclassified — check layer names.")

    # --- weighted overlay → LSI -----------------------------------------------
    lsi = weighted_overlay(suitability, registry)

    # --- exclusion mask --------------------------------------------------------
    exc_mask: xr.DataArray | None = layers.get("exclusion_mask")

    # --- classify LSI ---------------------------------------------------------
    class_raster = classify_lsi(lsi, n_classes=5, method="quantile")

    # --- extract sites --------------------------------------------------------
    _dist = layers.get("dist_power")
    dist_ptl: xr.DataArray | None = _dist if _dist is not None else layers.get("dist_ptl")
    sites: gpd.GeoDataFrame = extract_sites(
        lsi,
        class_raster,
        top_classes=(5, 4),
        min_area_km2=0.5,
        distance_to_ptl=dist_ptl,
        top_k=10,
    )

    # --- persist all artefacts in memory (before saving to disk) -------------
    # Key: layer_name, Value: DataArray to render + save bounds for
    named_layers: dict[str, xr.DataArray] = {"lsi": lsi, "class_raster": class_raster}
    if exc_mask is not None:
        named_layers["exclusion_mask"] = exc_mask

    # Render PNGs and save bounds from the in-memory DataArrays
    for layer_name, da in named_layers.items():
        try:
            png_bytes = _render_png(da, layer_name)
            (record.job_dir / f"{layer_name}.png").write_bytes(png_bytes)
        except Exception as exc:
            log.warning("Could not render PNG for layer %s: %s", layer_name, exc)
        _save_bounds(record.job_dir, layer_name, da)

    # Persist layers to disk (best-effort; failures are non-fatal)
    for layer_name, da in named_layers.items():
        _save_layer(record.job_dir, layer_name, da)

    # --- reproject sites to WGS-84 and save -----------------------------------
    if len(sites) > 0:
        wgs84 = CRS.from_epsg(4326)
        sites_wgs84 = sites.to_crs(wgs84) if sites.crs is not None and sites.crs != wgs84 else sites
        # Add centroid lat/lon for WGS-84 representation
        sites_out = _enrich_sites(sites_wgs84, record.aoi, layers)
        record.n_sites = len(sites_out)
        sites_out.to_file(record.job_dir / "sites.geojson", driver="GeoJSON")
    else:
        record.n_sites = 0
        # Write empty GeoJSON
        (record.job_dir / "sites.geojson").write_text('{"type":"FeatureCollection","features":[]}')


def _criterion_key_to_layer_name(key: str) -> str:
    """Map a criterion key (from criteria.yaml) to the layer name in the Dataset."""
    mapping: dict[str, str] = {
        "solar_radiation": "ghi_annual",
        "slope": "slope",
        "aspect": "aspect_class",
        "aspect_class": "aspect_class",
        "shadow": "shadow",  # may not be present
        "lulc": "lulc",
        "temperature": "temperature",
        "humidity": "humidity",
        "wind_speed": "wind_speed",
        "dist_ptl": "dist_power",
        "dist_roads": "dist_roads",
        "dist_railway": "dist_railway",
        "dist_urban": "dist_urban",
        "land_capability": "land_capability",  # may not be present
        "elevation": "elevation",
    }
    return mapping.get(key, key)


def _enrich_sites(
    sites: gpd.GeoDataFrame,
    aoi: AOI,
    layers: dict[str, xr.DataArray],
) -> gpd.GeoDataFrame:
    """Ensure required columns exist; compute energy for top site if TMY available.

    ``aoi`` and ``layers`` are reserved for future energy enrichment (P2.4).
    """
    _ = aoi  # reserved for future energy enrichment
    _ = layers  # reserved for future energy enrichment
    return sites


def _save_layer(job_dir: Path, name: str, da: xr.DataArray) -> None:
    """Persist a DataArray to NetCDF in the job directory.

    Drops rioxarray-added coordinates (spatial_ref) that have string dtype and
    cause netCDF4 encoding errors, while preserving x/y coords and the CRS
    string in attrs so _save_bounds can reconstruct the WGS-84 extent.
    """
    try:
        out = job_dir / f"{name}.nc"
        da_save = da.copy()

        # Save CRS info into attrs before stripping spatial_ref
        crs_str: str | None = None
        if da_save.rio.crs is not None:
            crs_str = da_save.rio.crs.to_string()
        elif "crs" in da_save.attrs:
            crs_str = str(da_save.attrs["crs"])

        # Drop rioxarray-added string coords that break netCDF encoding
        coords_to_drop = [
            c for c in da_save.coords if da_save.coords[c].dtype.kind in ("U", "S", "O")
        ]
        if coords_to_drop:
            da_save = da_save.drop_vars(coords_to_drop)

        # Keep only serialisable attrs
        da_save.attrs = {
            k: v
            for k, v in da_save.attrs.items()
            if isinstance(v, (str, int, float)) or (isinstance(v, list) and len(v) < 100)
        }
        if crs_str:
            da_save.attrs["crs"] = crs_str

        da_save.to_netcdf(out)
    except Exception as exc:
        log.warning("Could not save layer %s: %s", name, exc)


def _save_bounds(job_dir: Path, name: str, da: xr.DataArray) -> None:
    """Compute and save the WGS-84 bounding box for a layer."""
    try:
        crs_str = None
        if da.rio.crs is not None:
            crs_str = da.rio.crs.to_string()
        elif "crs" in da.attrs:
            crs_str = da.attrs["crs"]

        x = da.coords["x"].values
        y = da.coords["y"].values
        minx, maxx = float(x.min()), float(x.max())
        miny, maxy = float(y.min()), float(y.max())

        if crs_str and "4326" not in crs_str:
            transformer = Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)
            lon_min, lat_min = transformer.transform(minx, miny)
            lon_max, lat_max = transformer.transform(maxx, maxy)
        else:
            lon_min, lat_min, lon_max, lat_max = minx, miny, maxx, maxy

        bounds = {
            "west": float(lon_min),
            "south": float(lat_min),
            "east": float(lon_max),
            "north": float(lat_max),
        }
        (job_dir / f"{name}.bounds.json").write_text(json.dumps(bounds))
    except Exception as exc:
        log.warning("Could not save bounds for layer %s: %s", name, exc)


def _render_png(da: xr.DataArray, layer_name: str) -> bytes:
    """Render a DataArray as a colormapped PNG using matplotlib.

    Returns PNG bytes.
    """
    import matplotlib.pyplot as plt  # lazy: mpl.use("Agg") already called at module level

    data = np.array(da.values, dtype=np.float64)

    # Handle 3D arrays (e.g. band dim)
    if data.ndim == 3:
        data = data[0]
    elif data.ndim != 2:
        raise ValueError(f"Cannot render layer {layer_name}: ndim={data.ndim}")

    # Replace sentinel nodata with NaN
    data = data.copy()
    data[data == -9999] = np.nan

    # Choose colormap by layer type
    if "exclusion" in layer_name or "mask" in layer_name:
        cmap = "Reds"
    elif "lsi" in layer_name or "suit" in layer_name:
        cmap = "RdYlGn"
    elif "slope" in layer_name:
        cmap = "YlOrRd"
    elif "elevation" in layer_name:
        cmap = "terrain"
    else:
        cmap = "viridis"

    fig, ax = plt.subplots(figsize=(6, 6), dpi=100)
    im = ax.imshow(data, cmap=cmap, interpolation="nearest")
    ax.axis("off")
    plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    plt.tight_layout(pad=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Job Registry
# ---------------------------------------------------------------------------


class JobRegistry:
    """In-process registry of all submitted jobs.

    Thread-safety: asyncio single-threaded; dict access is safe.
    """

    def __init__(self, jobs_root: Path | None = None) -> None:
        self._jobs: dict[str, _JobRecord] = {}
        self._jobs_root = jobs_root or _DEFAULT_JOBS_ROOT
        # Keep strong references to tasks so they are not garbage-collected
        # before they complete (RUF006).
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(
        self,
        aoi: AOI,
        resolution_m: int,
        weight_overrides: dict[str, float] | None,
        layer_provider: LayerProviderFn | None = None,
    ) -> str:
        """Create a job, enqueue it, return job_id."""
        job_id = uuid.uuid4().hex
        record = _JobRecord(job_id, aoi, resolution_m, weight_overrides, self._jobs_root)
        self._jobs[job_id] = record

        provider = layer_provider or default_layer_provider
        task = asyncio.create_task(_run_pipeline(record, provider))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    def get(self, job_id: str) -> _JobRecord | None:
        return self._jobs.get(job_id)

    def job_dir(self, job_id: str) -> Path | None:
        rec = self._jobs.get(job_id)
        return rec.job_dir if rec else None
