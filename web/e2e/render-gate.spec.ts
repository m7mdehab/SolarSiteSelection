import { test, expect, type Page } from '@playwright/test';

// Render-level gate: API "job done + sites.geojson 200" is NOT proof the UI
// renders. These tests assert the results view actually displays (LSI legend +
// ranking table) and that the flow produces NO uncaught exceptions — the exact
// regression that white-screened the deployed app (`.toFixed()` on an undefined
// site field). One test runs with complete data; one runs with sites MISSING
// the energy fields and asserts the app degrades gracefully (no white screen).

const TINY_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

const CRITERIA = {
  groups: {
    g: {
      name: 'Economic',
      weight: 1.0,
      criteria: [
        {
          key: 'ghi',
          name: 'GHI',
          group: 'g',
          kind: 'raster',
          local_weight: 1.0,
          global_weight: 1.0,
          data_source: 'PVGIS',
          unit: 'kWh/m2/day',
          reclassification: 'higher is better',
        },
      ],
    },
  },
  lsi_classes: [
    { id: 1, label: 'Least suitable', description: '' },
    { id: 2, label: 'Marginally', description: '' },
    { id: 3, label: 'Moderately', description: '' },
    { id: 4, label: 'Highly', description: '' },
    { id: 5, label: 'Most suitable', description: '' },
  ],
  hard_exclusion_rules: [],
};

function siteFeature(rank: number, withEnergy: boolean) {
  const base: Record<string, unknown> = {
    rank,
    area_km2: 12.5 - rank,
    mean_lsi: 0.82 - rank * 0.05,
    max_lsi: 0.95,
    centroid_lon: 27.5,
    centroid_lat: 31.25,
  };
  if (withEnergy) {
    base.kwh_per_kwp_yr = 1850 - rank * 50;
    base.gwh_per_yr = 23.1 - rank;
    base.lcoe = 0.032 + rank * 0.001;
  }
  return {
    type: 'Feature',
    properties: base,
    geometry: {
      type: 'Polygon',
      coordinates: [[[27.4, 31.2], [27.6, 31.2], [27.6, 31.3], [27.4, 31.3], [27.4, 31.2]]],
    },
  };
}

async function mockApi(page: Page, withEnergy: boolean): Promise<void> {
  let jobCalls = 0;
  await page.route('**/criteria', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
  );
  await page.route('**/analyze', (r) =>
    r.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ job_id: 'j1', status: 'queued' }),
    }),
  );
  await page.route('**/jobs/j1', (r) => {
    jobCalls += 1;
    const done = jobCalls >= 2;
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        job_id: 'j1',
        status: done ? 'done' : 'acquiring',
        acquire_stages: [{ source: 'PVGIS', status: done ? 'done' : 'running' }],
        analysis_status: done ? 'done' : 'pending',
        n_sites: done ? 2 : 0,
        skipped_sources: [],
      }),
    });
  });
  await page.route('**/jobs/j1/layers/lsi.png', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'image/png',
      body: TINY_PNG,
      headers: { 'X-Layer-Bounds': JSON.stringify({ west: 27, south: 31, east: 28, north: 31.5 }) },
    }),
  );
  await page.route('**/jobs/j1/layers/lsi.bounds', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ west: 27, south: 31, east: 28, north: 31.5 }),
    }),
  );
  await page.route('**/jobs/j1/sites.geojson', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        type: 'FeatureCollection',
        features: [siteFeature(1, withEnergy), siteFeature(2, withEnergy)],
      }),
    }),
  );
}

function trackErrors(page: Page): string[] {
  const errs: string[] = [];
  page.on('pageerror', (e) => errs.push('PAGEERROR: ' + (e.message || String(e))));
  page.on('console', (m) => {
    if (m.type() === 'error') errs.push('CONSOLE: ' + m.text());
  });
  return errs;
}

// Uncaught exceptions (white-screen cause). Tile/network console noise is excluded.
function uncaught(errs: string[]): string[] {
  return errs.filter(
    (e) => e.startsWith('PAGEERROR') || /TypeError|Cannot read|is not a function|undefined/.test(e),
  );
}

async function runFlow(page: Page): Promise<void> {
  await page.goto('/');
  // The app now leads with the rooftop view; switch to utility siting first.
  await page.getByTestId('mode-utility').click();
  await expect(page.getByTestId('run-analysis-btn')).toBeVisible();
  await page.getByTestId('aoi-preset-select').selectOption('nw-coast-egypt');
  await page.getByTestId('run-analysis-btn').click();
  await expect(page.locator('.progress-status-done')).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId('ranking-table')).toBeVisible({ timeout: 10000 });
}

test.describe('RENDER GATE — results view must actually display', () => {
  test('LSI map + legend render and the flow has no uncaught errors', async ({ page }) => {
    const errs = trackErrors(page);
    await mockApi(page, true);
    await runFlow(page);

    // The headline output: the LSI 5-class legend is present and non-empty.
    await expect(page.getByTestId('lsi-legend')).toBeVisible();
    await expect(page.locator('.lsi-legend-item')).toHaveCount(5);
    // The map overlay layer panel + a site row render.
    await expect(page.getByTestId('layer-panel')).toBeVisible();
    await expect(page.getByTestId('site-row-1')).toBeVisible();

    // The app did NOT white-screen.
    const rootLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0);
    expect(rootLen).toBeGreaterThan(100);

    // No uncaught exceptions during the whole flow.
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('sites MISSING energy fields degrade gracefully (no white screen)', async ({ page }) => {
    const errs = trackErrors(page);
    await mockApi(page, false); // sites without kwh/gwh/lcoe — the regression scenario
    await runFlow(page);

    // App must still render the table (with N/A), not unmount to a blank screen.
    await expect(page.getByTestId('ranking-table')).toBeVisible();
    await expect(page.getByTestId('site-yield-1')).toContainText('N/A');
    const rootLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0);
    expect(rootLen).toBeGreaterThan(100);

    // The missing field must NOT throw an uncaught exception.
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });
});
