import { test, expect } from '@playwright/test';

// Tiny 1x1 PNG
const TINY_PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
const TINY_PNG_BUF = Buffer.from(TINY_PNG_B64, 'base64');

const FIXTURE_CRITERIA = {
  groups: {
    solar: {
      name: 'Solar Resources',
      weight: 0.5,
      criteria: [
        {
          key: 'ghi',
          name: 'Global Horizontal Irradiance',
          group: 'solar',
          kind: 'raster',
          local_weight: 0.6,
          global_weight: 0.3,
          data_source: 'NASA POWER',
          unit: 'kWh/m2/day',
          reclassification: 'higher is better',
        },
        {
          key: 'slope',
          name: 'Slope',
          group: 'solar',
          kind: 'raster',
          local_weight: 0.4,
          global_weight: 0.2,
          data_source: 'SRTM',
          unit: 'degrees',
          reclassification: 'lower is better',
        },
      ],
    },
    infrastructure: {
      name: 'Infrastructure',
      weight: 0.5,
      criteria: [
        {
          key: 'distance_road',
          name: 'Distance to Road',
          group: 'infrastructure',
          kind: 'raster',
          local_weight: 1.0,
          global_weight: 0.5,
          data_source: 'OSM',
          unit: 'km',
          reclassification: 'lower is better',
        },
      ],
    },
  },
  lsi_classes: [
    { id: 1, label: 'Very Low', description: 'Unsuitable' },
    { id: 2, label: 'Low', description: 'Low suitability' },
    { id: 3, label: 'Moderate', description: 'Moderate suitability' },
    { id: 4, label: 'High', description: 'High suitability' },
    { id: 5, label: 'Very High', description: 'Best sites' },
  ],
  hard_exclusion_rules: [],
};

const FIXTURE_SITES = {
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {
        rank: 1,
        area_km2: 12.5,
        mean_lsi: 0.82,
        max_lsi: 0.95,
        centroid_lon: 27.5,
        centroid_lat: 31.25,
        kwh_per_kwp_yr: 1850,
        gwh_per_yr: 23.1,
        lcoe: 0.032,
      },
      geometry: {
        type: 'Polygon',
        coordinates: [[[27.4, 31.2], [27.6, 31.2], [27.6, 31.3], [27.4, 31.3], [27.4, 31.2]]],
      },
    },
    {
      type: 'Feature',
      properties: {
        rank: 2,
        area_km2: 8.3,
        mean_lsi: 0.74,
        max_lsi: 0.88,
        centroid_lon: 27.8,
        centroid_lat: 31.35,
        kwh_per_kwp_yr: 1720,
        gwh_per_yr: 14.3,
        lcoe: 0.038,
      },
      geometry: {
        type: 'Polygon',
        coordinates: [[[27.7, 31.3], [27.9, 31.3], [27.9, 31.4], [27.7, 31.4], [27.7, 31.3]]],
      },
    },
  ],
};

test.describe('Solar Site Selection acceptance test', () => {
  let jobCallCount = 0;

  test.beforeEach(async ({ page }) => {
    jobCallCount = 0;

    // Mock /criteria
    await page.route('**/criteria', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(FIXTURE_CRITERIA),
      });
    });

    // Mock /analyze
    await page.route('**/analyze', (route) => {
      void route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({ job_id: 'test-job-123', status: 'queued' }),
      });
    });

    // Mock /jobs/test-job-123
    await page.route('**/jobs/test-job-123', (route) => {
      jobCallCount += 1;
      const isDone = jobCallCount >= 2;
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          isDone
            ? {
                job_id: 'test-job-123',
                status: 'done',
                acquire_stages: [
                  { source: 'NASA POWER', status: 'done' },
                  { source: 'SRTM', status: 'done' },
                  { source: 'OSM', status: 'done' },
                ],
                analysis_status: 'done',
                n_sites: 2,
                skipped_sources: [],
              }
            : {
                job_id: 'test-job-123',
                status: 'acquiring',
                acquire_stages: [
                  { source: 'NASA POWER', status: 'running' },
                  { source: 'SRTM', status: 'pending' },
                  { source: 'OSM', status: 'pending' },
                ],
                analysis_status: 'pending',
                n_sites: 0,
                skipped_sources: [],
              }
        ),
      });
    });

    // Mock layer PNG
    await page.route('**/jobs/test-job-123/layers/lsi.png', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: TINY_PNG_BUF,
        headers: {
          'X-Layer-Bounds': JSON.stringify({ west: 27.0, south: 31.0, east: 28.0, north: 31.5 }),
        },
      });
    });

    // Mock layer bounds
    await page.route('**/jobs/test-job-123/layers/lsi.bounds', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ west: 27.0, south: 31.0, east: 28.0, north: 31.5 }),
      });
    });

    // Mock sites GeoJSON
    await page.route('**/jobs/test-job-123/sites.geojson', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(FIXTURE_SITES),
      });
    });
  });

  test('full analysis flow: preset AOI -> run -> results -> site detail', async ({ page }) => {
    await page.goto('/');

    // Wait for the app to load and criteria to be fetched
    await expect(page.getByTestId('run-analysis-btn')).toBeVisible();

    // Select preset AOI
    const presetSelect = page.getByTestId('aoi-preset-select');
    await presetSelect.selectOption('nw-coast-egypt');

    // Verify area feedback appears
    await expect(page.getByTestId('aoi-area-feedback')).toBeVisible();

    // Run analysis
    const runBtn = page.getByTestId('run-analysis-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();

    // Wait for done status (the job endpoint returns done on 2nd call)
    await expect(page.getByTestId('progress-view')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.progress-status-done')).toBeVisible({ timeout: 15000 });

    // Wait for ranking table to appear
    await expect(page.getByTestId('ranking-table')).toBeVisible({ timeout: 10000 });

    // Layer panel should show
    await expect(page.getByTestId('layer-panel')).toBeVisible();

    // Click on site row 1 in ranking table
    const site1Row = page.getByTestId('site-row-1');
    await expect(site1Row).toBeVisible();
    await site1Row.click();

    // Verify site detail shows yield
    await expect(page.getByTestId('site-yield-1')).toBeVisible();
    const yieldText = await page.getByTestId('site-yield-1').textContent();
    expect(yieldText).toContain('1850');

    // Also verify the site popup appears in the right panel
    await expect(page.getByTestId('site-popup')).toBeVisible();
    await expect(page.getByTestId('site-yield')).toBeVisible();
    const popupYield = await page.getByTestId('site-yield').textContent();
    expect(popupYield).toContain('1850');
  });
});
