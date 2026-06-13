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
}

export interface SiteFeature extends GeoJSON.Feature {
  properties: SiteProperties;
}

export interface SitesGeoJSON extends GeoJSON.FeatureCollection {
  features: SiteFeature[];
}
