"""Validate the SolarSiteSelection engine against Habib et al. 2020.

Reference
---------
Habib, S. M., Suliman, A. E. E., Al Nahry, A. H., & Abd El Rahman, E. N. (2020).
    Spatial modeling for the optimum site selection of solar photovoltaics power
    plant in the northwest coast of Egypt.  *Remote Sensing Applications: Society
    and Environment*, 18, 100313.
    https://doi.org/10.1016/j.rsase.2020.100313

Public quantitative anchor
--------------------------
The only verbatim public figure from the paper is:
    "24.9% (261.1747 km²) of the investigation area is more suitable"
and the 5-class LSI scheme (most / highly / moderately / marginally / least
suitable).  The paper's study area is the NW coast (Alexandria + Matrouh),
approximately 1,048 km².

The paper's full per-class table and pairwise AHP matrices are NOT publicly
available (paywalled).  We therefore use our documented MCDA default weights
from ``configs/criteria.yaml`` and compare only the published "more suitable"
fraction against our own top-class and top-two-class percentages.

Usage
-----
Run from the repository root with the cache already seeded::

    uv run python scripts/validate_against_paper.py

All inputs come from ``data/cache/``; no network access is required.  This is
the default (``--offline`` is implied).  To regenerate the cache from live APIs::

    uv run python scripts/demo_aoi.py --aoi tests/fixtures/nw_coast_aoi.geojson \\
        --resolution 500
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # non-interactive backend — safe in CI and headless environments
import matplotlib.colors
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr

# ---------------------------------------------------------------------------
# Path setup: let this script import from the src/ tree when invoked directly.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from solarsite.acquire.base import grid_for_aoi  # noqa: E402
from solarsite.acquire.climate import ClimateSource  # noqa: E402
from solarsite.acquire.landcover import WDPASource, WorldCoverSource, exclusion_mask  # noqa: E402
from solarsite.acquire.osm import (  # noqa: E402
    OSMPowerSource,
    OSMRailwaySource,
    OSMRoadsSource,
    OSMUrbanSource,
    proximity_for,
)
from solarsite.acquire.pvgis import PVGISSource  # noqa: E402
from solarsite.acquire.terrain import TerrainSource  # noqa: E402
from solarsite.analysis.overlay import (  # noqa: E402
    apply_exclusions,
    build_exclusion_mask,
    classify_lsi,
    weighted_overlay,
)
from solarsite.analysis.reclassify import reclassify_layer  # noqa: E402
from solarsite.analysis.registry import CriteriaRegistry, load_registry  # noqa: E402
from solarsite.core import AOI, DiskCache  # noqa: E402

# ---------------------------------------------------------------------------
# Published reference anchor
# ---------------------------------------------------------------------------
PAPER_MORE_SUITABLE_PCT: float = 24.9
"""Verbatim public figure from Habib et al. 2020: percentage of study area
classified as 'more suitable'.  Source: abstract/publicly visible text."""

PAPER_MORE_SUITABLE_KM2: float = 261.1747
"""km² corresponding to the 24.9% figure (Habib 2020 public)."""

PAPER_STUDY_AREA_KM2: float = 1048.0
"""Approximate study-area size implied by the two figures above (km²)."""

# ---------------------------------------------------------------------------
# Aspect-class integer → label mapping (terrain module produces 0-8 codes)
# criteria.yaml uses string labels: flat, north, northeast, east, southeast,
# south, southwest, west, northwest.
# ---------------------------------------------------------------------------
_ASPECT_INT_TO_LABEL: dict[int, str] = {
    0: "flat",
    1: "north",
    2: "northeast",
    3: "east",
    4: "southeast",
    5: "south",
    6: "southwest",
    7: "west",
    8: "northwest",
}

# ---------------------------------------------------------------------------
# LSI class labels (5 = most suitable)
# ---------------------------------------------------------------------------
_LSI_CLASS_LABELS: dict[int, str] = {
    5: "Most suitable",
    4: "Highly suitable",
    3: "Moderately suitable",
    2: "Marginally suitable",
    1: "Least suitable",
}

# ---------------------------------------------------------------------------
# Colour map for the LSI map PNG
# ---------------------------------------------------------------------------
_LSI_CMAP = matplotlib.colors.ListedColormap(
    ["#d73027", "#f46d43", "#fee090", "#74add1", "#313695"],
    name="lsi_5class",
)
# Values 1-5; set nodata (-9999) to transparent white via norm and extend.


def _load_aoi(aoi_path: Path) -> AOI:
    return AOI.from_geojson(json.loads(aoi_path.read_text()))


def _build_dataset(
    aoi: AOI,
    resolution_m: int,
    cache: DiskCache,
    *,
    offline: bool = True,
) -> tuple[xr.Dataset, list[str]]:
    """Acquire every available layer, stack into one aligned Dataset.

    Mirrors ``scripts/demo_aoi.py::build_dataset``; kept here so this script
    is self-contained and importable by the test suite without depending on the
    demo script at runtime.
    """
    grid = grid_for_aoi(aoi, resolution_m)
    layers: dict[str, xr.DataArray] = {}
    skipped: list[str] = []

    def _acquire(label: str, cache_names: list[str], thunk: object) -> None:
        import sys as _sys

        if offline and not all(
            cache.exists(n, aoi.hash, {"resolution_m": resolution_m}) for n in cache_names
        ):
            print(f"  [skip] {label}: not in cache (offline)", file=_sys.stderr)
            skipped.append(label)
            return
        try:
            result = thunk() if callable(thunk) else {}  # type: ignore[operator]
            for name, da in result.items():  # type: ignore[union-attr]
                layers[name] = da
        except Exception as exc:
            print(f"  [skip] {label}: {type(exc).__name__}: {exc}", file=_sys.stderr)
            skipped.append(label)

    def _split_bands(da: xr.DataArray, bands: tuple[str, ...]) -> dict[str, xr.DataArray]:
        return {b: da.sel(band=b).drop_vars("band") for b in bands}

    _acquire(
        "solar",
        ["pvgis"],
        lambda: {"ghi_annual": PVGISSource(cache=cache).fetch(aoi, resolution_m)},
    )
    _acquire(
        "terrain",
        ["terrain"],
        lambda: _split_bands(
            TerrainSource(cache=cache).fetch(aoi, resolution_m),
            ("elevation", "slope", "aspect_class"),
        ),
    )
    _acquire(
        "climate",
        ["openmeteo"],
        lambda: _split_bands(
            ClimateSource(cache=cache).fetch(aoi, resolution_m),
            ("temperature", "humidity", "wind_speed"),
        ),
    )

    wc_source = WorldCoverSource(cache=cache)
    _acquire("lulc", ["worldcover"], lambda: {"lulc": wc_source.fetch(aoi, resolution_m)})
    _acquire(
        "exclusion_mask",
        ["worldcover", "wdpa"],
        lambda: {
            "exclusion_mask": exclusion_mask(
                aoi,
                resolution_m,
                worldcover_source=wc_source,
                wdpa_source=WDPASource(cache=cache),
            )
        },
    )

    osm_sources: dict[str, object] = {
        "dist_power": OSMPowerSource(cache=cache),
        "dist_roads": OSMRoadsSource(cache=cache),
        "dist_railway": OSMRailwaySource(cache=cache),
        "dist_urban": OSMUrbanSource(cache=cache),
    }
    for var, src in osm_sources.items():
        # Capture loop variables explicitly to avoid closure pitfall.
        _acquire(
            var,
            [src.name],  # type: ignore[union-attr]
            lambda v=var, s=src: {v: proximity_for(s, aoi, resolution_m)},  # type: ignore[arg-type]
        )

    if not layers:
        raise SystemExit("No layers could be loaded from cache.")

    ref = next(iter(layers.values()))
    aligned = {
        name: da.assign_coords(x=ref.x, y=ref.y) if da.shape == ref.shape else da
        for name, da in layers.items()
    }
    ds = xr.Dataset(aligned)
    ds.attrs["crs"] = f"EPSG:{grid.crs.to_epsg()}"
    ds.attrs["resolution_m"] = resolution_m
    ds.attrs["aoi_hash"] = aoi.hash
    ds.attrs["aoi_area_km2"] = round(aoi.area_km2, 1)
    return ds, skipped


def _reclassify_all(
    ds: xr.Dataset,
    registry: CriteriaRegistry,
    resolution_m: int,
) -> dict[str, xr.DataArray]:
    """Reclassify every available factor criterion into a [0,1] suitability layer.

    Handles the layer-name mismatches between the Dataset (produced by the
    acquire module) and the criterion keys in the registry:

    * ``ghi_annual``  → criterion ``solar_radiation``  (annual kWh/m²/yr)
    * ``slope``       → criterion ``slope``
    * ``aspect_class``→ criterion ``aspect``  (int codes 0-8 → label lookup)
    * ``temperature`` → criterion ``temperature``
    * ``humidity``    → criterion ``humidity``
    * ``wind_speed``  → criterion ``wind_speed``
    * ``lulc``        → criterion ``lulc``
    * ``dist_power``  → criterion ``dist_ptl``   (metres → km÷1000)
    * ``dist_roads``  → criterion ``dist_roads`` (metres → km)
    * ``dist_railway``→ criterion ``dist_railway`` (metres → km)
    * ``dist_urban``  → criterion ``dist_urban``  (metres → km)

    Criteria without a matching layer (``shadow``, ``land_capability``) are
    skipped; ``weighted_overlay`` renormalises over the present criteria.
    """
    suit: dict[str, xr.DataArray] = {}

    def _get(var: str) -> xr.DataArray | None:
        return ds.get(var, None)  # type: ignore[return-value]

    # --- solar_radiation (GHI annual → breakpoints in kWh/m²/day) -----------
    ghi = _get("ghi_annual")
    if ghi is not None:
        crit = registry.criterion("solar_radiation")
        suit["solar_radiation"] = reclassify_layer(ghi, crit, data_unit="kWh/m2/yr")

    # --- slope ---------------------------------------------------------------
    slope = _get("slope")
    if slope is not None:
        crit = registry.criterion("slope")
        suit["slope"] = reclassify_layer(slope, crit)

    # --- aspect: integer codes → string labels → class-score lookup ----------
    aspect_int = _get("aspect_class")
    if aspect_int is not None:
        crit = registry.criterion("aspect")
        # Map integer codes to string labels so the ClassScoreReclassification
        # can look them up in its dict (keys: "flat","north","south", …).
        aspect_labels = np.vectorize(_ASPECT_INT_TO_LABEL.get)(
            aspect_int.values.astype(int), "flat"
        )
        # Build a DataArray with string values isn't directly supported in
        # reclassify_layer (which calls .values.copy().astype(float64)); instead
        # we convert the string labels to integer index and supply a mapped spec.
        # Simpler: manually apply class_scores directly.
        label_to_score: dict[str, float] = {}
        from solarsite.analysis.registry import ClassScoreReclassification

        reclass = crit.reclassification
        if isinstance(reclass, ClassScoreReclassification):
            label_to_score = reclass.class_scores
        default_score = 0.0
        aspect_scores = np.array(
            [label_to_score.get(lbl, default_score) for lbl in aspect_labels.ravel()],
            dtype=np.float64,
        ).reshape(aspect_int.shape)
        suit["aspect"] = xr.DataArray(
            aspect_scores,
            dims=aspect_int.dims,
            coords=aspect_int.coords,
            name="suit_aspect",
            attrs={
                "long_name": "Suitability: Aspect",
                "criterion_key": "aspect",
                "units": "suitability_score_0_1",
            },
        )

    # --- temperature ---------------------------------------------------------
    temp = _get("temperature")
    if temp is not None:
        crit = registry.criterion("temperature")
        suit["temperature"] = reclassify_layer(temp, crit)

    # --- humidity ------------------------------------------------------------
    hum = _get("humidity")
    if hum is not None:
        crit = registry.criterion("humidity")
        suit["humidity"] = reclassify_layer(hum, crit)

    # --- wind_speed ----------------------------------------------------------
    wind = _get("wind_speed")
    if wind is not None:
        crit = registry.criterion("wind_speed")
        suit["wind_speed"] = reclassify_layer(wind, crit)

    # --- lulc (ESA WorldCover integer codes) ---------------------------------
    lulc = _get("lulc")
    if lulc is not None:
        crit = registry.criterion("lulc")
        suit["lulc"] = reclassify_layer(lulc.astype(np.float64), crit)

    # --- OSM distance layers (metres → km) -----------------------------------
    _osm_mapping: list[tuple[str, str]] = [
        ("dist_power", "dist_ptl"),
        ("dist_roads", "dist_roads"),
        ("dist_railway", "dist_railway"),
        ("dist_urban", "dist_urban"),
    ]
    for ds_var, crit_key in _osm_mapping:
        raw = _get(ds_var)
        if raw is not None:
            crit = registry.criterion(crit_key)
            km_layer = raw / 1000.0  # convert metres → km
            km_layer = km_layer.assign_attrs({"units": "km"})
            suit[crit_key] = reclassify_layer(km_layer, crit)

    # Note: 'shadow' and 'land_capability' have no matching cached layer;
    # weighted_overlay will renormalise over the 11 present criteria.
    return suit


def _build_additional_exclusions(
    ds: xr.Dataset,
    registry: CriteriaRegistry,
) -> xr.DataArray:
    """Derive additional distance-based exclusion masks from cached layers.

    The ``exclusion_mask`` from the acquire module already covers WorldCover
    hard exclusions (urban=50, water=80) and WDPA.  Here we add the
    distance-buffer exclusions stated in criteria.yaml:

    * PTL buffer < 4.8 km (excl_ptl_buffer)
    * Road buffer < 150 m (excl_road_buffer)
    * Railway buffer < 150 m (excl_railway_buffer)
    * Urban buffer < 1.5 km (excl_urban_buffer)
    * Steep slope > 20° (excl_slope)

    All are OR'd into the existing exclusion_mask.
    """
    # Start with the pre-computed mask (WorldCover + WDPA)
    base_mask: xr.DataArray | None = ds.get("exclusion_mask", None)  # type: ignore[assignment]

    masks: dict[str, xr.DataArray] = {}

    # PTL buffer: dist_power < 4800 m
    if "dist_power" in ds:
        masks["excl_ptl_buffer"] = (ds["dist_power"] < 4800.0).astype(np.uint8)

    # Road buffer: dist_roads < 150 m
    if "dist_roads" in ds:
        masks["excl_road_buffer"] = (ds["dist_roads"] < 150.0).astype(np.uint8)

    # Railway buffer: dist_railway < 150 m
    if "dist_railway" in ds:
        masks["excl_railway_buffer"] = (ds["dist_railway"] < 150.0).astype(np.uint8)

    # Urban buffer: dist_urban < 1500 m
    if "dist_urban" in ds:
        masks["excl_urban_buffer"] = (ds["dist_urban"] < 1500.0).astype(np.uint8)

    # Steep slope: slope > 20°
    if "slope" in ds:
        masks["excl_slope"] = (ds["slope"] > 20.0).astype(np.uint8)

    if not masks and base_mask is None:
        # Nothing to combine; return all-zero mask matching first variable shape
        ref = next(iter(ds.data_vars.values()))
        zero = xr.DataArray(
            np.zeros(ref.shape, dtype=np.uint8),
            dims=ref.dims,
            coords=ref.coords,
            name="exclusion_mask",
        )
        return zero

    if masks:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            extra_mask = build_exclusion_mask(masks, registry)
        if base_mask is not None:
            combined = ((base_mask != 0) | (extra_mask != 0)).astype(np.uint8)
            combined.name = "exclusion_mask"
            return combined
        return extra_mask

    return base_mask  # type: ignore[return-value]


def _compute_class_percentages(
    lsi_class: xr.DataArray,
) -> dict[int, float]:
    """Return {class_id: pct_of_valid_cells} for classes 1-5.

    Nodata cells (-9999) are excluded from the denominator so the percentages
    sum to 100% over the non-excluded study-area cells.
    """
    vals = lsi_class.values.ravel()
    valid = vals[vals != -9999]
    total = valid.size
    pct: dict[int, float] = {}
    for cls in range(1, 6):
        count = int(np.sum(valid == cls))
        pct[cls] = round(100.0 * count / total, 2) if total > 0 else 0.0
    return pct


def _write_csv(
    pct: dict[int, float],
    out_dir: Path,
    resolution_m: int,
    aoi_area_km2: float,
) -> Path:
    """Write the class-percentage table as a CSV file."""
    csv_path = out_dir / "lsi_class_distribution.csv"
    lines = [
        "class_id,label,pct_of_valid_area,area_km2",
    ]
    for cls in range(5, 0, -1):
        label = _LSI_CLASS_LABELS[cls]
        p = pct.get(cls, 0.0)
        area_km2 = round(p / 100.0 * aoi_area_km2, 2)
        lines.append(f"{cls},{label},{p:.2f},{area_km2:.2f}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path


def _write_markdown_table(
    pct: dict[int, float],
    out_dir: Path,
    aoi_area_km2: float,
) -> Path:
    """Write the class-percentage table as a Markdown fragment."""
    md_path = out_dir / "lsi_class_table.md"
    lines = [
        "| Class | Label | % of valid area | Area (km²) |",
        "|------:|-------|----------------:|-----------:|",
    ]
    for cls in range(5, 0, -1):
        label = _LSI_CLASS_LABELS[cls]
        p = pct.get(cls, 0.0)
        area_km2 = round(p / 100.0 * aoi_area_km2, 2)
        lines.append(f"| {cls} | {label} | {p:.2f}% | {area_km2:.1f} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def _write_lsi_map(
    lsi_class: xr.DataArray,
    out_dir: Path,
    *,
    aoi_area_km2: float,
    resolution_m: int,
) -> Path:
    """Write a colormapped LSI-class map PNG to *out_dir*."""
    png_path = out_dir / "lsi_map.png"

    data = lsi_class.values.astype(float)
    data[data == -9999] = np.nan  # nodata → NaN for masked display

    fig, ax = plt.subplots(figsize=(8, 5), dpi=120)

    im = ax.imshow(
        data,
        cmap=_LSI_CMAP,
        vmin=0.5,
        vmax=5.5,
        interpolation="nearest",
        origin="upper",
    )

    # Colorbar with class labels
    cbar = fig.colorbar(im, ax=ax, ticks=[1, 2, 3, 4, 5], shrink=0.8)
    cbar.ax.set_yticklabels(
        [
            "1 - Least suitable",
            "2 - Marginally suitable",
            "3 - Moderately suitable",
            "4 - Highly suitable",
            "5 - Most suitable",
        ],
        fontsize=7,
    )

    ax.set_title(
        f"LSI Suitability Map — NW Coast Egypt\n"
        f"AOI {aoi_area_km2:.0f} km²  |  {resolution_m} m resolution  "
        f"|  equal-interval classification\n"
        f"(Weights: documented MCDA defaults — NOT Habib 2020 paywalled weights)",
        fontsize=8,
    )
    ax.set_xlabel("x (grid columns)")
    ax.set_ylabel("y (grid rows)")

    fig.tight_layout()
    fig.savefig(png_path, bbox_inches="tight", dpi=120)
    plt.close(fig)
    return png_path


def run_validation(
    aoi_path: Path,
    cache_dir: Path,
    out_dir: Path,
    resolution_m: int = 500,
    *,
    offline: bool = True,
    classify_method: str = "equal_interval",
    verbose: bool = True,
) -> dict[str, object]:
    """Run the full validation pipeline and return a results dict.

    Parameters
    ----------
    aoi_path:
        Path to the NW-coast AOI GeoJSON fixture.
    cache_dir:
        Path to the DiskCache root (``data/cache/``).
    out_dir:
        Output directory for CSV, Markdown table, and PNG map.
    resolution_m:
        Grid resolution in metres (default 500 to match cache).
    offline:
        If True (default), read only from cache.
    classify_method:
        ``"equal_interval"`` (default) or ``"quantile"``.
        Equal-interval is used as primary because it maps the continuous LSI
        range uniformly to 5 bands; quantile always produces 20% per class by
        construction and is less informative for paper comparison.
    verbose:
        Print progress messages to stdout.

    Returns
    -------
    dict with keys:
        ``pct``: {class_id: pct}
        ``top1_pct``: % in class 5 (most suitable)
        ``top2_pct``: % in classes 4+5
        ``paper_anchor_pct``: PAPER_MORE_SUITABLE_PCT (24.9)
        ``delta_top1``: our top-class % minus paper anchor
        ``delta_top2``: our top-two-class % minus paper anchor
        ``aoi_area_km2``: AOI area
        ``n_valid_cells``: number of non-excluded grid cells
        ``n_total_cells``: total grid cells
        ``excluded_pct``: % of cells excluded
    """
    if verbose:
        print(f"[validate] AOI: {aoi_path}")
        print(f"[validate] Cache: {cache_dir}")
        print(f"[validate] Output: {out_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Load AOI and registry -------------------------------------------
    aoi = _load_aoi(aoi_path)
    registry = load_registry()
    if verbose:
        print(f"[validate] AOI area: {aoi.area_km2:.1f} km²")

    # --- 2. Load Dataset from cache -----------------------------------------
    cache = DiskCache(cache_dir)
    ds, skipped = _build_dataset(aoi, resolution_m, cache, offline=offline)
    if verbose:
        print(f"[validate] Layers loaded: {list(ds.data_vars.keys())}")
        if skipped:
            print(f"[validate] Skipped: {skipped}")

    # --- 3. Build additional exclusion mask ---------------------------------
    full_excl_mask = _build_additional_exclusions(ds, registry)

    n_total = int(full_excl_mask.size)
    n_excluded = int(np.sum(full_excl_mask.values != 0))
    excluded_pct = round(100.0 * n_excluded / n_total, 1) if n_total > 0 else 0.0
    if verbose:
        print(f"[validate] Total cells: {n_total}  Excluded: {n_excluded} ({excluded_pct}%)")

    # --- 4. Reclassify all criteria -----------------------------------------
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        suit_layers = _reclassify_all(ds, registry, resolution_m)

    if verbose:
        print(f"[validate] Suitability layers computed: {list(suit_layers.keys())}")

    # --- 5. Weighted overlay → continuous LSI --------------------------------
    lsi = weighted_overlay(suit_layers, registry)
    if verbose:
        valid_lsi = lsi.values[~np.isnan(lsi.values)]
        print(f"[validate] LSI range: {valid_lsi.min():.4f} - {valid_lsi.max():.4f}")

    # --- 6. Apply exclusions -------------------------------------------------
    lsi_masked = apply_exclusions(lsi, full_excl_mask, fill_value=float("nan"))

    # --- 7. Classify LSI → 5-class map (equal_interval primary) -------------
    lsi_class = classify_lsi(lsi_masked, n_classes=5, method=classify_method)  # type: ignore[arg-type]

    # --- 8. Compute class-percentage distribution ----------------------------
    pct = _compute_class_percentages(lsi_class)

    n_valid = int(np.sum(lsi_class.values != -9999))
    top1_pct = pct.get(5, 0.0)
    top2_pct = round(pct.get(5, 0.0) + pct.get(4, 0.0), 2)

    if verbose:
        print("\n[validate] LSI class distribution (% of valid non-excluded area):")
        total_check = 0.0
        for cls in range(5, 0, -1):
            label = _LSI_CLASS_LABELS[cls]
            p = pct.get(cls, 0.0)
            total_check += p
            print(f"  Class {cls} ({label:25s}): {p:6.2f}%")
        print(f"  Total: {total_check:.2f}%")

    # --- 9. Write output artefacts ------------------------------------------
    csv_path = _write_csv(pct, out_dir, resolution_m, aoi.area_km2)
    md_path = _write_markdown_table(pct, out_dir, aoi.area_km2)
    png_path = _write_lsi_map(
        lsi_class, out_dir, aoi_area_km2=aoi.area_km2, resolution_m=resolution_m
    )

    png_size_kb = png_path.stat().st_size // 1024

    if verbose:
        print("\n[validate] Artifacts written:")
        print(f"  CSV:      {csv_path}")
        print(f"  Markdown: {md_path}")
        print(f"  PNG map:  {png_path}  ({png_size_kb} KB)")
        print(
            f"\n[validate] Comparison with Habib et al. 2020 public anchor "
            f"({PAPER_MORE_SUITABLE_PCT}% 'more suitable'):"
        )
        print(f"  Our top-class   (class 5):        {top1_pct:.2f}%")
        print(f"  Our top-two     (classes 4+5):     {top2_pct:.2f}%")
        delta1 = top1_pct - PAPER_MORE_SUITABLE_PCT
        delta2 = top2_pct - PAPER_MORE_SUITABLE_PCT
        print(f"  Delta top-class vs paper:         {delta1:+.2f} pp")
        print(f"  Delta top-two   vs paper:         {delta2:+.2f} pp")

    return {
        "pct": pct,
        "top1_pct": top1_pct,
        "top2_pct": top2_pct,
        "paper_anchor_pct": PAPER_MORE_SUITABLE_PCT,
        "delta_top1": round(top1_pct - PAPER_MORE_SUITABLE_PCT, 2),
        "delta_top2": round(top2_pct - PAPER_MORE_SUITABLE_PCT, 2),
        "aoi_area_km2": round(aoi.area_km2, 1),
        "n_valid_cells": n_valid,
        "n_total_cells": n_total,
        "excluded_pct": excluded_pct,
        "skipped_sources": skipped,
        "lsi_class": lsi_class,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--aoi",
        type=Path,
        default=_REPO_ROOT / "tests" / "fixtures" / "nw_coast_aoi.geojson",
        help="AOI GeoJSON path (default: tests/fixtures/nw_coast_aoi.geojson)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_REPO_ROOT / "data" / "cache",
        help="DiskCache root (default: data/cache)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_REPO_ROOT / "docs" / "validation",
        help="Output directory for artefacts (default: docs/validation)",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=500,
        help="Grid resolution in metres (default: 500; must match cache)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=True,
        help="Read only from cache (default true; always offline unless --no-offline)",
    )
    parser.add_argument(
        "--no-offline",
        dest="offline",
        action="store_false",
        help="Allow network access for cache misses",
    )
    parser.add_argument(
        "--classify-method",
        choices=["equal_interval", "quantile"],
        default="equal_interval",
        help="LSI classification method (default: equal_interval)",
    )
    args = parser.parse_args(argv)

    run_validation(
        aoi_path=args.aoi,
        cache_dir=args.cache_dir,
        out_dir=args.out_dir,
        resolution_m=args.resolution,
        offline=args.offline,
        classify_method=args.classify_method,
        verbose=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
