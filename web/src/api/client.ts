import type {
  CriteriaResponse,
  AhpCheckRequest,
  AhpCheckResponse,
  AnalyzeRequest,
  AnalyzeResponse,
  JobResponse,
  LayerBounds,
  SitesGeoJSON,
  RooftopRequest,
  RooftopResult,
  GeocodeResponse,
  VersionInfo,
} from '../types/api';

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string) || '';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export async function getCriteria(): Promise<CriteriaResponse> {
  return request<CriteriaResponse>('/criteria');
}

export async function checkAhp(data: AhpCheckRequest): Promise<AhpCheckResponse> {
  return request<AhpCheckResponse>('/ahp/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function startAnalysis(data: AnalyzeRequest): Promise<AnalyzeResponse> {
  return request<AnalyzeResponse>('/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function getJob(jobId: string): Promise<JobResponse> {
  return request<JobResponse>(`/jobs/${jobId}`);
}

export async function getLayerBounds(jobId: string, layerName: string): Promise<LayerBounds> {
  return request<LayerBounds>(`/jobs/${jobId}/layers/${layerName}.bounds`);
}

export function getLayerPngUrl(jobId: string, layerName: string): string {
  return `${BASE_URL}/jobs/${jobId}/layers/${layerName}.png`;
}

export async function getSites(jobId: string): Promise<SitesGeoJSON> {
  return request<SitesGeoJSON>(`/jobs/${jobId}/sites.geojson`);
}

export function getReportPdfUrl(jobId: string): string {
  return `${BASE_URL}/jobs/${jobId}/report.pdf`;
}

export function getSitesGeoJsonUrl(jobId: string): string {
  return `${BASE_URL}/jobs/${jobId}/sites.geojson`;
}

export async function analyzeRooftop(data: RooftopRequest): Promise<RooftopResult> {
  return request<RooftopResult>('/consumer/rooftop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function getRecommendedRanges(): Promise<Record<string, Record<string, string>>> {
  return request<Record<string, Record<string, string>>>('/consumer/recommended-ranges');
}

export async function geocode(q: string): Promise<GeocodeResponse> {
  return request<GeocodeResponse>(`/geocode?q=${encodeURIComponent(q)}`);
}

export async function getVersion(): Promise<VersionInfo> {
  return request<VersionInfo>('/version');
}
