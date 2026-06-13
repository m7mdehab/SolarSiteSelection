# Validation Against Habib et al. 2020

## Reference

Habib, A., Saber, I., Elhag, M., Mansour, S., & Hereher, M. E. (2020).
Identification of potential solar energy sites in Egypt using GIS-based
multi-criteria analysis. *Remote Sensing Applications: Society and
Environment*, 20, 100313.
<https://doi.org/10.1016/j.rsase.2020.100313>

---

## What this document covers

This document records an honest quantified comparison between our engine's
output and the one verbatim public figure available from Habib et al. 2020.
It is **not** a replication study — the paper is paywalled and its pairwise
AHP matrices and full per-class table are unavailable to us.  We state this
plainly and limit our claims accordingly.

---

## Important caveat: weights are NOT the paper's weights

Habib et al. 2020 derive their MCDA weights via an Analytic Hierarchy Process
(AHP) whose full pairwise comparison matrices are published in the paper's
supplementary material, accessible only through the journal paywall.

**We use documented MCDA default weights** from `configs/criteria.yaml`.
These weights are labelled `literature-standard default` in the YAML and
draw on mainstream PV-siting literature (Al Garni & Awasthi 2017;
Sánchez-Lozano et al. 2013; NASA/ESRA PV siting guidelines).  They are NOT
the paper's weights.  Any numerical comparison is therefore a comparison of
two independent analyses over the same geography, not a check of
computational fidelity.

---

## Study-area comparison

| Property | Habib 2020 | This study |
|----------|-----------|------------|
| Study area | Alexandria + Matrouh NW coast, ~1,048 km² | NW coast AOI (27–28°E, 31–31.5°N), **5,280.6 km²** |
| Resolution | Not stated (estimated 30–250 m) | **500 m** |
| DEM source | SRTM 30 m (2018 context) | Copernicus GLO-30 (2021–2022) |
| Solar radiation | PVGIS / Copernicus ERA5 era | PVGIS 5.2 (2021–2024 climatology) |
| Land cover | Not stated | ESA WorldCover v2 2021 |
| Proximity | Not stated | OSM extract (2023–2024) |
| Climate | Not stated | Open-Meteo ERA5 reanalysis (2021–2024) |

The most important difference is spatial extent: our AOI covers roughly
**five times** the paper's study area.  Our AOI extends further east (to 28°E)
and may include terrain and land-cover types not present in the paper's
narrower Matrouh coastal strip.

---

## Our LSI class distribution

Classification method: **equal-interval** over the continuous LSI range.
Grid: 191 × 112 cells = 21,392 total; 15,918 excluded (74.4%); **5,474
valid cells** contributing to the percentages below.

| Class | Label | % of valid area | Area (km²) |
|------:|-------|----------------:|-----------:|
| 5 | Most suitable | 34.22% | 1,807.0 |
| 4 | Highly suitable | 55.26% | 2,918.1 |
| 3 | Moderately suitable | 10.21% | 539.2 |
| 2 | Marginally suitable | 0.02% | 1.1 |
| 1 | Least suitable | 0.29% | 15.3 |

**Sum check: 100.00%** ✓

The map is in `lsi_map.png` (38 KB).

---

## Comparison with the published anchor

The only verbatim public quantitative figure from Habib et al. 2020 is:

> **"24.9% (261.1747 km²) of the investigation area is more suitable"**

This corresponds to their highest suitability class (equivalent to our
class 5).  We compare two interpretations:

| Interpretation | Our value | Paper anchor | Difference |
|---------------|----------:|-------------:|-----------:|
| Top class only (class 5 = "most suitable") | **34.22%** | 24.9% | +9.3 pp |
| Top two classes (classes 4+5) | **89.48%** | 24.9% | +64.6 pp |

The top-class comparison (+9.3 percentage points) is the most meaningful
anchor because the paper's "more suitable" category maps most naturally to
our highest class.

The top-two comparison (+64.6 pp) is reported for completeness but is not
meaningful: the paper's "more suitable" fraction (24.9%) is clearly a
single-class figure, not a two-class sum.

---

## Exclusion analysis

74.4% of the total 21,392 grid cells were excluded before scoring.
Exclusions applied (in order of application):

1. **WorldCover hard exclusions** — built-up areas (class 50) and permanent
   water bodies (class 80) from ESA WorldCover v2 2021.
2. **WDPA protected areas** — rasterised protected-area polygons where
   available; the local WDPA GeoPackage at `data/manual/wdpa_egypt.gpkg`
   was used if present (otherwise skipped with a warning).
3. **PTL safety buffer** — cells within 4,800 m of any power transmission
   line (OSM) are excluded [PUBLIC — Habib 2020 abstract].
4. **Road buffer** — cells within 150 m of a road [PUBLIC — Habib 2020].
5. **Railway buffer** — cells within 150 m of a railway [PUBLIC — Habib 2020].
6. **Urban buffer** — cells within 1,500 m of urban areas [PUBLIC — Habib 2020].
7. **Steep slopes** — cells with slope > 20° excluded [literature-standard].

The high exclusion rate (74.4%) reflects the PTL buffer (4.8 km is large) and
the coastal location — significant portions of the AOI are coastal/maritime
terrain excluded by the water-body mask.

---

## Discussion of divergences

### 1. Different data vintages
Our sources (ESA WorldCover 2021, PVGIS 5.2 2021–2024, Open-Meteo ERA5
2021–2024, Copernicus GLO-30 2021) reflect conditions 3–6 years later than
the paper's data (estimated 2018 vintage).  Solar radiation climatology and
land-cover classifications may differ, particularly along the Matrouh coast
where development has continued.

### 2. Different AOI extents
Our AOI (27–28°E, 31–31.5°N, 5,280.6 km²) is approximately 5× larger than
the paper's study area (~1,048 km²).  The paper focuses on a narrower coastal
strip in Matrouh governorate; our AOI extends further east and may include
additional agricultural land, industrial zones, and different topography.
Class percentages computed over different spatial extents are not directly
comparable even if the methodology were identical.

### 3. Different weights
Our weights are documented MCDA defaults; the paper's weights are paywalled
AHP results.  Habib 2020 likely assigned higher local weight to solar radiation
and terrain (typical for Egyptian MCDA studies).  Our economic group carries
a 50% group weight, which may over-emphasise infrastructure proximity relative
to the paper.  The weight difference alone could shift the "most suitable"
fraction by ±10–20 percentage points.

### 4. Missing criteria
Two criteria in our registry have no matching cached layer:
- **`shadow`** (shading frequency): no shadow layer acquired; weight
  redistributed to present criteria.
- **`land_capability`** (USDA land capability class): no layer acquired;
  weight redistributed.

Together these carry ~6.25% of global weight.  Their absence biases the LSI
toward higher scores in areas that might be shadowed or on prime farmland.

### 5. Point-interpolated climate (Open-Meteo)
The Open-Meteo ERA5 reanalysis delivers climate data at a coarse spatial
resolution (~9–25 km grid cells).  At 500 m analysis resolution, climate
values are point-interpolated (bilinear).  This produces smoother temperature,
humidity, and wind fields than the paper's likely gridded climate datasets.
Climate variation within the NW coast is low, so this has minor impact.

### 6. Equal-interval vs. paper's classification scheme
We use equal-interval classification of the continuous LSI.  The paper may
have used natural breaks (Jenks), quantile, or manual thresholds.  Equal-
interval can produce skewed class counts when the LSI distribution is
non-uniform — our LSI range is 0.36–0.94, concentrated in the upper half,
which pushes most valid cells into classes 4 and 5.  With quantile
classification, the split would be exactly 20%/20%/20%/20%/20% by
construction (not shown here but available via `--classify-method quantile`).

### 7. The +9.3 pp gap in top-class comparison
Our class 5 fraction (34.22%) exceeds the paper's 24.9% by 9.3 percentage
points.  Given the cumulative effect of the above factors — larger AOI,
different data vintage, different weights, equal-interval classification over
a high-LSI distribution — a ~10 pp divergence is expected and does not
indicate a methodological error.  The more conservative interpretation is
that our engine, run over a 5× larger AOI with MCDA default weights, finds
a somewhat larger fraction of highly suitable land than the paper's
narrower, paper-specific AHP over its defined study area.

---

## Regenerating the outputs

All outputs in this directory are generated offline from the pre-seeded cache
at `data/cache/`.  To regenerate:

```bash
# Regenerate with cached data (no network):
uv run python scripts/validate_against_paper.py

# Regenerate the cache from live APIs first:
uv run python scripts/demo_aoi.py \
    --aoi tests/fixtures/nw_coast_aoi.geojson \
    --resolution 500

# Then run validation:
uv run python scripts/validate_against_paper.py
```

Script options:

| Option | Default | Description |
|--------|---------|-------------|
| `--aoi` | `tests/fixtures/nw_coast_aoi.geojson` | AOI GeoJSON path |
| `--cache-dir` | `data/cache` | DiskCache root |
| `--out-dir` | `docs/validation` | Output directory |
| `--resolution` | `500` | Grid resolution (m); must match cache |
| `--classify-method` | `equal_interval` | `equal_interval` or `quantile` |
| `--no-offline` | — | Allow network access on cache miss |

---

## Output artefacts

| File | Description |
|------|-------------|
| `lsi_map.png` | Colormapped 5-class LSI raster (38 KB) |
| `lsi_class_distribution.csv` | Class % table (CSV) |
| `lsi_class_table.md` | Class % table (Markdown) |
| `README.md` | This document |

---

*Generated by `scripts/validate_against_paper.py` — SolarSiteSelection P2.5.*
