# Methodology

This document translates the criteria registry (`configs/criteria.yaml`) into
prose, explains the Analytic Hierarchy Process (AHP) implementation, and records
which thresholds derive from the publicly-accessible text of Habib et al. 2020
versus mainstream MCDA literature.

---

## Reference

Habib, S. M., Suliman, A. E. E., Al Nahry, A. H., & Abd El Rahman, E. N. (2020).
Spatial modeling for the optimum site selection of solar photovoltaics power
plant in the northwest coast of Egypt. *Remote Sensing Applications: Society and
Environment*, 18, 100313. <https://doi.org/10.1016/j.rsase.2020.100313>

Supporting MCDA literature:
- Al Garni, H. Z., & Awasthi, A. (2017). Solar PV power plant site selection
  using a GIS-AHP based approach with application in Saudi Arabia. *Applied
  Energy*, 206, 1225–1240.
- Sánchez-Lozano, J. M., Teruel-Solano, J., Soto-Elvira, P. L., & García-Cascales,
  M. S. (2013). Geographical Information Systems (GIS) and Multi-Criteria
  Decision Making (MCDM) methods for the evaluation of solar farms locations.
  *Renewable and Sustainable Energy Reviews*, 24, 544–556.
- Saaty, T. L. (1980/2008). *The Analytic Hierarchy Process*. McGraw-Hill /
  RWS Publications.

---

## Provenance legend

Throughout this document, thresholds are labelled:

- **PUBLIC (Habib 2020 abstract)** — value confirmed in the publicly-accessible
  text (abstract or publicly-visible supplementary material) of Habib et al. 2020.
- **Literature-standard default** — value from mainstream PV-siting MCDA
  literature (Al Garni & Awasthi 2017; Sánchez-Lozano et al. 2013); not
  specific to Habib 2020.

The paper's full pairwise AHP comparison matrices are published in supplementary
material accessible only through the journal paywall. **The weights used in this
engine are documented MCDA defaults, not the paper's weights.**

---

## Criteria groups and global weights

The twelve sub-criteria are organised into three groups. Group weights assign
the economic dimension the greatest importance, reflecting the dominant role of
infrastructure proximity and land cost in utility-scale PV project viability.

| Group | Weight | Rationale |
|-------|-------:|-----------|
| Economic | 0.50 | Grid connection CAPEX, O&M access cost, and land acquisition cost dominate project economics for utility-scale PV. |
| Technical | 0.25 | Solar resource and terrain govern yield potential and construction feasibility. |
| Environmental | 0.25 | Land cover, temperature, humidity, and wind affect module performance and regulatory exposure. |

The global weight of each criterion is `group_weight × local_weight`.

---

## Economic group (group weight = 0.50)

### Distance to Power Transmission Lines (dist_ptl)

- **Data source:** OSM Overpass — `power=line` features
- **Unit:** km
- **Local weight:** 0.30 (literature-standard default; highest economic weight
  because grid connection cost dominates CAPEX)
- **Global weight:** 0.15

Breakpoints:

| Distance (km) | Score | Provenance |
|----------:|------:|-----------|
| < 4.8 | 0.00 | **PUBLIC (Habib 2020 abstract)** — hard safety/interference buffer |
| 4.8–10 | 1.00 | Literature-standard default |
| 10–20 | 0.75 | Literature-standard default |
| 20–35 | 0.50 | Literature-standard default |
| 35–50 | 0.25 | Literature-standard default |
| > 50 | 0.00 | **PUBLIC (Habib 2020 abstract)** — prohibitive connection cost |

Cells within 4.8 km of a transmission line are also applied as a **hard
exclusion** (score forced to 0 before the weighted overlay).

### Distance to Roads (dist_roads)

- **Data source:** OSM Overpass — `highway` features
- **Unit:** km
- **Local weight:** 0.25 (literature-standard default)
- **Global weight:** 0.125

Breakpoints:

| Distance (km) | Score | Provenance |
|----------:|------:|-----------|
| < 0.15 | 0.00 | **PUBLIC (Habib 2020 abstract)** — 150 m safety exclusion buffer |
| 0.15–5 | 1.00 | Literature-standard default |
| 5–15 | 0.75 | Literature-standard default |
| 15–30 | 0.50 | Literature-standard default |
| 30–50 | 0.25 | Literature-standard default |
| > 50 | 0.00 | **PUBLIC (Habib 2020 abstract)** |

### Distance to Railway (dist_railway)

- **Data source:** OSM Overpass — `railway=rail` features
- **Unit:** km
- **Local weight:** 0.15 (literature-standard default)
- **Global weight:** 0.075

Breakpoints are identical in structure to dist_roads: 150 m hard exclusion
buffer (**PUBLIC**), 0-score beyond 50 km (**PUBLIC**), with literature-standard
intermediate bands.

### Distance to Urban Areas (dist_urban)

- **Data source:** OSM Overpass — place/landuse urban polygons
- **Unit:** km
- **Local weight:** 0.15 (literature-standard default)
- **Global weight:** 0.075

Breakpoints:

| Distance (km) | Score | Provenance |
|----------:|------:|-----------|
| < 1.5 | 0.00 | **PUBLIC (Habib 2020 abstract)** — hard exclusion buffer |
| 1.5–10 | 1.00 | Literature-standard default |
| 10–25 | 0.75 | Literature-standard default |
| 25–40 | 0.50 | Literature-standard default |
| > 40 | 0.25 | Literature-standard default |

Close proximity to urban load centres reduces transmission losses and land cost,
but the inner 1.5 km is excluded for safety and planning reasons.

### Land Capability Class (land_capability)

- **Data source:** No global public API available; layer not yet acquired.
  Weight redistributed to present criteria during scoring.
- **Unit:** USDA Land Capability Class (I–VIII)
- **Local weight:** 0.15 (literature-standard default)
- **Global weight:** 0.075

Class scores (literature-standard defaults):

| Class | Score | Interpretation |
|-------|------:|----------------|
| I | 0.00 | Prime farmland — avoid |
| II | 0.10 | |
| III | 0.25 | |
| IV | 0.50 | |
| V | 0.65 | |
| VI | 0.80 | |
| VII | 0.90 | |
| VIII | 1.00 | Barren/wasteland — best for PV |

---

## Technical group (group weight = 0.25)

### Solar Radiation (solar_radiation)

- **Data source:** PVGIS 5.2 MRcalc endpoint (2005–2020 climatology)
- **Unit:** kWh/m²/day (PVGIS delivers kWh/m²/yr; divided by 365 at
  reclassification)
- **Local weight:** 0.40 (literature-standard default; GHI is the primary
  yield driver)
- **Global weight:** 0.10

Breakpoints:

| GHI (kWh/m²/day) | Score | Provenance |
|------:|------:|-----------|
| < 3.5 | 0.00 | Literature-standard default |
| 3.5–4.0 | 0.10 | Literature-standard default |
| 4.0–4.7 | 0.25 | **PUBLIC (Habib 2020 abstract)** — study-area lower bound |
| 4.7–5.0 | 0.50 | Literature-standard default (within PUBLIC study-area range) |
| 5.0–5.4 | 0.75 | Literature-standard default (within PUBLIC study-area range) |
| 5.4–5.9 | 1.00 | **PUBLIC (Habib 2020 abstract)** — study-area upper bound |
| > 5.9 | 1.00 | Literature-standard default |

The study-area GHI range of 4.7–5.9 kWh/m²/day is confirmed in the Habib 2020
abstract. Intermediate score bands within that range are literature-standard
defaults, not from the paper.

### Slope (slope)

- **Data source:** Copernicus GLO-30 DEM → central-difference gradient
- **Unit:** degrees
- **Local weight:** 0.25 (literature-standard default)
- **Global weight:** 0.0625

Breakpoints:

| Slope (°) | Score | Provenance |
|----------:|------:|-----------|
| < 5 | 1.00 | **PUBLIC (Habib 2020 abstract)** — most suitable |
| 5–10 | 0.75 | Literature-standard default |
| 10–15 | 0.40 | Literature-standard default |
| 15–20 | 0.10 | Literature-standard default |
| > 20 | 0.00 | Literature-standard default (hard exclusion also applied at > 20°) |

### Aspect (aspect)

- **Data source:** Copernicus GLO-30 DEM → 8-way compass class
- **Unit:** compass class
- **Local weight:** 0.20 (literature-standard default)
- **Global weight:** 0.05

Class scores (Northern Hemisphere convention, literature-standard defaults):

| Direction | Score |
|-----------|------:|
| Flat | 1.00 |
| South | 1.00 |
| Southwest | 0.80 |
| Southeast | 0.80 |
| West | 0.60 |
| East | 0.60 |
| Northwest | 0.30 |
| Northeast | 0.30 |
| North | 0.00 |

### Shadow / Shading Frequency (shadow)

- **Data source:** Terrain proxy (not yet acquired as a dedicated layer).
  Weight redistributed to present criteria during scoring.
- **Unit:** fraction of daylight hours with shadowing (0–1)
- **Local weight:** 0.15 (literature-standard default)
- **Global weight:** 0.0375

Breakpoints (literature-standard defaults): < 5% shadowed hours → score 1.0,
declining linearly through 0.80, 0.50, 0.25 to 0.0 for > 35% shadowing.

---

## Environmental group (group weight = 0.25)

### Land Use / Land Cover (lulc)

- **Data source:** ESA WorldCover v2 2021 (10 m), resampled to analysis grid
  using nearest-neighbour
- **Unit:** ESA WorldCover v2 integer class code
- **Local weight:** 0.40 (literature-standard default)
- **Global weight:** 0.10

Class scores (literature-standard defaults):

| ESA Code | Class | Score |
|---------|-------|------:|
| 10 | Tree cover | 0.00 |
| 20 | Shrubland | 0.00 |
| 30 | Grassland | 0.40 |
| 40 | Cropland | 0.00 |
| 50 | Built-up | 0.00 (hard exclusion) |
| 60 | Bare/sparse vegetation | 1.00 |
| 70 | Snow and ice | 0.00 |
| 80 | Permanent water bodies | 0.00 (hard exclusion) |
| 90 | Herbaceous wetland | 0.00 |
| 95 | Mangroves | 0.00 |
| 100 | Moss and lichen | 0.70 |

Classes 50 (built-up) and 80 (water bodies) are also enforced as **hard
exclusions** before the weighted overlay.

### Mean Annual Temperature (temperature)

- **Data source:** Open-Meteo ERA5 reanalysis archive, year 2023
- **Unit:** degrees Celsius
- **Local weight:** 0.20 (literature-standard default)
- **Global weight:** 0.05

PV module efficiency degrades approximately 0.4–0.5%/°C above 25°C (literature-
standard default). Breakpoints score cooler climates higher: < 15°C → 1.0,
declining through 0.85, 0.70, 0.50, 0.30, 0.15 to 0.0 above 40°C.

### Mean Annual Relative Humidity (humidity)

- **Data source:** Open-Meteo ERA5 reanalysis archive, year 2023
- **Unit:** percent
- **Local weight:** 0.20 (literature-standard default)
- **Global weight:** 0.05

Lower humidity reduces soiling losses and module degradation. Breakpoints:
< 20% → 1.0, declining through 0.85, 0.65, 0.40, 0.20 to 0.0 above 80%.
All literature-standard defaults.

### Mean Annual Wind Speed (wind_speed)

- **Data source:** Open-Meteo ERA5 reanalysis archive, year 2023
- **Unit:** m/s
- **Local weight:** 0.20 (literature-standard default)
- **Global weight:** 0.05

Moderate wind aids passive module cooling; very high wind creates structural
risk. Breakpoints: 2–4 m/s → 1.0 (moderate, good cooling), declining to 0.0
above 10 m/s. All literature-standard defaults.

---

## Hard-exclusion constraints

Hard exclusions are applied as binary masks before any weighted scoring. Cells
that intersect a hard-exclusion condition receive LSI = 0 regardless of factor
scores.

| Exclusion | Source | Condition | Provenance |
|-----------|--------|-----------|-----------|
| WDPA Protected Areas | WDPA GeoPackage | Intersects protected polygon | Literature-standard |
| Permanent water bodies | ESA WorldCover | Class 80 | Literature-standard |
| Built-up core | ESA WorldCover | Class 50 | Literature-standard |
| Urban buffer | OSM urban | Distance < 1.5 km | **PUBLIC (Habib 2020 abstract)** |
| Steep slopes | Copernicus GLO-30 | Slope > 20° | Literature-standard |
| Road safety buffer | OSM roads | Distance < 150 m | **PUBLIC (Habib 2020 abstract)** |
| Railway safety buffer | OSM railway | Distance < 150 m | **PUBLIC (Habib 2020 abstract)** |
| PTL safety buffer | OSM power | Distance < 4.8 km | **PUBLIC (Habib 2020 abstract)** |

---

## The 5-class LSI scheme

The continuous LSI (0–1) is classified into five labeled classes using
equal-interval bins over the observed LSI range. The scheme is confirmed in
the publicly-accessible text of Habib et al. 2020 (**PUBLIC**).

| Class ID | Label | Description |
|---------:|-------|-------------|
| 5 | most_suitable | Most suitable |
| 4 | highly_suitable | Highly suitable |
| 3 | moderately_suitable | Moderately suitable |
| 2 | marginally_suitable | Marginally suitable |
| 1 | least_suitable | Least suitable |

Alternative classification methods (e.g. quantile, natural breaks) are
available via the `--classify-method` argument of `scripts/validate_against_paper.py`.

---

## Analytic Hierarchy Process (AHP)

The AHP implementation is in `src/solarsite/analysis/ahp.py`, following Saaty
(1980/2008).

### Algorithm

Weights are derived from a positive reciprocal pairwise comparison matrix
**A** (entries on the Saaty 1–9 scale) using the **principal eigenvector method
via power iteration**:

1. Initialise **w₀** as the column-normalised geometric means of **A**.
2. Each iteration: **w**_{k+1} = **A** · **w**_k, normalised to sum 1.
3. Converge when ‖**w**_{k+1} − **w**_k‖_∞ < 10⁻¹⁰ (or 1,000 iterations max).

**λ_max** is estimated by:

    λ_max = (1/n) · Σᵢ [(A · w)ᵢ / wᵢ]

**Consistency Index (CI)** and **Consistency Ratio (CR)**:

    CI = (λ_max − n) / (n − 1)
    CR = CI / RI[n]

where RI[n] is the Saaty Random Index table (reproduced below). A matrix is
accepted if CR ≤ 0.10.

### Saaty Random Index (RI) table

| n | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| RI | 0.00 | 0.00 | 0.58 | 0.90 | 1.12 | 1.24 | 1.32 | 1.41 | 1.45 | 1.49 |

Values from Saaty (1980/2008), used verbatim in `src/solarsite/analysis/ahp.py`.

### Consistency enforcement

`ahp_weights` returns an `AHPResult` including `consistent` (bool) and
`most_inconsistent` (i, j, k triple with the largest transitivity deviation
|a[i,k] − a[i,j]·a[j,k]|) for UI highlighting.

`ahp_weights_strict` raises `InconsistencyError` when CR > 0.10, carrying the
CR value and the most inconsistent triple so the front-end can guide users to
revise that specific judgment.

The POST `/ahp/check` endpoint exposes this API over HTTP.

### Runtime weight editing

The criteria registry defines default weights. These can be overridden at
analysis time by submitting a `weight_overrides` dict to `POST /analyze`. Any
overridden pairwise matrix with CR > 0.10 is rejected with HTTP 422 before the
analysis job is queued.

---

## Energy yield and LCOE

Energy estimation is in `src/solarsite/analysis/energy.py`, using
**pvlib ModelChain** on a PVGIS Typical Meteorological Year (TMY) for the
site centroid.

**System model:** Fixed-tilt at latitude, due south (180° azimuth), 1 kWp
reference DC module (`pdc0=1000 W`, `gamma_pdc=−0.004 %/°C`), SAPM
open-rack glass-glass temperature model, physical AOI model, no spectral
correction. System losses are encoded as `eta_inv_nom=0.96` (4% derate for
inverter efficiency, DC wiring, mismatch, and soiling).

**LCOE formula** (NREL SAM convention):

    CRF  = r · (1+r)ⁿ / ((1+r)ⁿ − 1)
    LCOE [USD/MWh] = (capex_per_kWp · CRF + opex_per_kWp_yr)
                      / specific_yield_kWh_kWp_yr × 1000

Default assumptions: 1,000 USD/kWp CAPEX, 17 USD/kWp/yr OPEX, 7% real WACC,
25-year lifetime, 45 MWp/km² packing density. All configurable via the
`EnergyAssumptions` model.

---

*See also: [`docs/architecture.md`](architecture.md) for the layered system
design, [`docs/validation/README.md`](validation/README.md) for quantified
comparison with the reference paper.*
