export interface Criterion {
  key: string;
  name: string;
  group: string;
  kind: string;
  local_weight: number;
  global_weight: number;
  data_source: string;
  unit: string;
  reclassification: string;
}

export interface CriterionGroup {
  name: string;
  weight: number;
  criteria: Criterion[];
}

export interface LsiClass {
  id: number;
  label: string;
  description: string;
}

export interface HardExclusionRule {
  key: string;
  name: string;
  kind: string;
  data_source: string;
  exclude_when: string;
  note: string;
}

export interface CriteriaResponse {
  groups: Record<string, CriterionGroup>;
  lsi_classes: LsiClass[];
  hard_exclusion_rules: HardExclusionRule[];
}

export interface AhpCheckRequest {
  matrix: number[][];
}

export interface AhpCheckResponse {
  weights: number[];
  lambda_max: number;
  ci: number;
  cr: number;
  consistent: boolean;
  most_inconsistent: [number, number, number] | null;
}

export interface AnalyzeRequest {
  aoi: GeoJSON.FeatureCollection | GeoJSON.Feature | GeoJSON.Polygon;
  resolution_m?: number;
  weight_overrides?: Record<string, number>;
}

export interface AnalyzeResponse {
  job_id: string;
  status: JobStatus;
}

export type JobStatus = 'queued' | 'acquiring' | 'analyzing' | 'done' | 'error';
export type StageStatus = 'pending' | 'running' | 'done' | 'failed';

export interface AcquireStage {
  source: string;
  status: StageStatus;
  error?: string;
}

export interface JobResponse {
  job_id: string;
  status: JobStatus;
  resolution_m?: number;
  acquire_stages: AcquireStage[];
  analysis_status?: StageStatus;
  analysis_error?: string;
  error?: string;
  n_sites?: number;
  skipped_sources?: string[];
  notes?: string[];
}

export interface LayerBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface SiteProperties {
  rank: number;
  area_km2: number;
  mean_lsi: number;
  max_lsi: number;
  centroid_lon: number;
  centroid_lat: number;
  kwh_per_kwp_yr: number;
  gwh_per_yr: number;
  lcoe: number;
  capacity_mwp?: number;
  // Which model produced the energy figures (E3):
  //   'pvlib_modelchain' = validation-grade; 'ghi_pr_offline' = offline estimate.
  energy_method?: string;
}

export interface SiteFeature extends GeoJSON.Feature {
  properties: SiteProperties;
}

export interface SitesGeoJSON extends GeoJSON.FeatureCollection {
  features: SiteFeature[];
}

// ---- Consumer rooftop mode -------------------------------------------------

export interface RooftopRequest {
  roof: {
    area_m2: number;
    usable_fraction?: number;
    module_efficiency?: number;
  };
  latitude?: number;
  longitude?: number;
  specific_yield_kwh_kwp_yr?: number;
  consumption?: {
    annual_kwh?: number | null;
    self_consumption_fraction?: number | null;
  };
  economics?: {
    install_cost_usd_per_w?: number | null;
    retail_tariff_usd_per_kwh?: number | null;
    export_rate_usd_per_kwh?: number | null;
    incentive_usd?: number | null;
    om_cost_usd_per_kw_yr?: number | null;
  };
}

export interface RooftopResult {
  energy: {
    capacity_kwp: number;
    specific_yield_kwh_kwp_yr: number;
    annual_production_kwh: number;
    self_consumed_kwh: number;
    exported_kwh: number;
    grid_import_kwh: number;
    self_consumption_ratio: number;
    self_sufficiency: number;
    dispatch_policy: string;
  };
  economics: {
    install_cost_usd: number | null;
    net_install_cost_usd: number | null;
    annual_savings_usd: number | null;
    simple_payback_years: number | null;
    npv_usd: number | null;
    lifetime_savings_usd: number | null;
    unverified_inputs: string[];
    caveats: string[];
  };
  sanity_ok: boolean;
  sanity_messages: string[];
  assumptions: string[];
  monthly_kwh: number[] | null;
  production_method: string | null;
  production_note: string | null;
  payback_band: { low: number; base: number; high: number; basis: string } | null;
  unverified_panel: string[];
}

// ---- Geocoding (consumer location search) ----------------------------------

export interface GeocodeResult {
  label: string;
  name: string;
  lat: number;
  lon: number;
  type: string;
}

export interface GeocodeResponse {
  query: string;
  results: GeocodeResult[];
  attribution: string;
  note?: string;
}

// ---- Deploy traceability ---------------------------------------------------

export interface VersionInfo {
  git_sha: string;
  git_describe: string | null;
  deployed_at: string | null;
  source: string;
  repo: string;
}
