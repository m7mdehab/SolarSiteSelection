import { test, expect, type Page, type Locator } from '@playwright/test';

// Interaction gate: exercise the flows through the SAME affordances a human uses —
// typing into the geocoder, clicking the map to drop a pin, drawing a utility AOI
// polygon, and tracing a roof polygon. A coordinate-injecting probe would miss the
// thing that actually mattered last round (a real person had no way to set a
// location). All external services are mocked; the basemap tiles are blocked so CI
// touches no live network. No flow may white-screen (zero uncaught exceptions).

const CAIRO_GEOCODE = {
  query: 'Cairo',
  attribution: '© OpenStreetMap contributors',
  results: [
    { label: 'Cairo, Egypt', name: 'Cairo', lat: 30.0444, lon: 31.2357, type: 'city' },
    { label: 'Cairo, Illinois, USA', name: 'Cairo', lat: 37.0053, lon: -89.1764, type: 'city' },
  ],
};

const EMPTY_GEOCODE = {
  query: 'zzzqqq',
  attribution: '© OpenStreetMap contributors',
  results: [],
  note: "Couldn't find that place — try again or click the map.",
};

const ROOFTOP_RESULT = {
  energy: {
    capacity_kwp: 15.0,
    specific_yield_kwh_kwp_yr: 1800.0,
    annual_production_kwh: 27000.0,
    self_consumed_kwh: 6000.0,
    exported_kwh: 21000.0,
    grid_import_kwh: 0.0,
    self_consumption_ratio: 0.22,
    self_sufficiency: 1.0,
    dispatch_policy: 'annual_net_metering',
  },
  economics: {
    install_cost_usd: null,
    net_install_cost_usd: null,
    annual_savings_usd: null,
    simple_payback_years: null,
    npv_usd: null,
    lifetime_savings_usd: null,
    unverified_inputs: ['install_cost_usd_per_w'],
    caveats: [],
  },
  sanity_ok: true,
  sanity_messages: [],
  assumptions: ['usable roof fraction = 0.75'],
  monthly_kwh: [1800, 1900, 2300, 2500, 2700, 2800, 2900, 2800, 2500, 2200, 1900, 1700],
  production_method: 'pvlib_modelchain',
  production_note: 'Production is validation-grade (pvlib ModelChain on the PVGIS TMY).',
  payback_band: null,
  unverified_panel: ['install_cost_usd_per_w: not verified for your area (enter your own value)'],
};

const CRITERIA = { groups: {}, lsi_classes: [], hard_exclusion_rules: [] };

const UTIL_CRITERIA = {
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
    { id: 1, label: 'Least', description: '' },
    { id: 2, label: 'Marginal', description: '' },
    { id: 3, label: 'Moderate', description: '' },
    { id: 4, label: 'High', description: '' },
    { id: 5, label: 'Most', description: '' },
  ],
  hard_exclusion_rules: [],
};

const UTIL_SITES = {
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
        capacity_mwp: 560,
      },
      geometry: {
        type: 'Polygon',
        coordinates: [[[27.4, 31.2], [27.6, 31.2], [27.6, 31.3], [27.4, 31.3], [27.4, 31.2]]],
      },
    },
  ],
};

function trackErrors(page: Page): string[] {
  const errs: string[] = [];
  page.on('pageerror', (e) => errs.push('PAGEERROR: ' + (e.message || String(e))));
  page.on('console', (m) => {
    if (m.type() === 'error') errs.push('CONSOLE: ' + m.text());
  });
  return errs;
}

function uncaught(errs: string[]): string[] {
  return errs.filter(
    (e) => e.startsWith('PAGEERROR') || /TypeError|Cannot read|is not a function/.test(e),
  );
}

// Block live basemap tiles so CI never touches an external service.
async function blockTiles(page: Page): Promise<void> {
  await page.route(/tile\.openstreetmap\.org/, (r) => r.abort());
}

async function rootLen(page: Page): Promise<number> {
  return page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0);
}

// Click the map at a fractional (fx, fy) position within its bounding box.
async function clickMapFrac(map: Locator, fx: number, fy: number): Promise<void> {
  const box = await map.boundingBox();
  if (!box) throw new Error('map has no bounding box');
  await map.page().mouse.click(box.x + box.width * fx, box.y + box.height * fy);
}

test.describe('INTERACTION GATE — human-style flows must work and never white-screen', () => {
  test('consumer: geocoder search → select suggestion → estimate renders', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/geocode**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CAIRO_GEOCODE) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ROOFTOP_RESULT) }),
    );

    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible(); // rooftop is the default

    await page.getByTestId('geocoder-input').fill('Cairo');
    const suggestion = page.getByTestId('geocoder-suggestion').first();
    await expect(suggestion).toBeVisible({ timeout: 5000 });
    await suggestion.click();
    await expect(page.getByTestId('cv-location-status')).toContainText('Cairo');

    await page.getByTestId('consumer-estimate-btn').click();
    await expect(page.getByTestId('cv-production')).toContainText('kWh');
    await expect(page.getByTestId('cv-method')).toContainText('validation-grade');

    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: geocode miss degrades gracefully (note, no white screen)', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/geocode**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_GEOCODE) }),
    );

    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('geocoder-input').fill('zzzqqq');
    await expect(page.getByTestId('geocoder-note')).toContainText("Couldn't find that place");

    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: click map to drop a pin → estimate renders', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ROOFTOP_RESULT) }),
    );

    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    const map = page.getByTestId('cv-map');
    await expect(map).toBeVisible();
    await page.waitForTimeout(700); // let the map canvas initialise

    await clickMapFrac(map, 0.5, 0.5);
    await expect(page.getByTestId('cv-location-status')).toContainText('Pinned');

    await page.getByTestId('consumer-estimate-btn').click();
    await expect(page.getByTestId('cv-production')).toContainText('kWh');

    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: draw a roof polygon → area populates', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );

    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    const map = page.getByTestId('cv-map');
    await expect(map).toBeVisible();
    await page.waitForTimeout(700);

    await page.getByTestId('cv-draw-roof-btn').click();
    await expect(page.getByTestId('cv-roof-hint')).toBeVisible();
    await clickMapFrac(map, 0.4, 0.4);
    await clickMapFrac(map, 0.6, 0.4);
    await clickMapFrac(map, 0.6, 0.6);
    await clickMapFrac(map, 0.4, 0.6);
    await page.getByTestId('cv-finish-roof-btn').click();

    await expect(page.getByTestId('cv-area-from-draw')).toBeVisible();
    const area = await page.getByTestId('cv-area').inputValue();
    expect(parseFloat(area)).toBeGreaterThan(0);

    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: oversized roof → plausibility warning, no nonsense headline crash', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    const huge = {
      ...ROOFTOP_RESULT,
      energy: { ...ROOFTOP_RESULT.energy, capacity_kwp: 35228 },
      sanity_ok: false,
      sanity_messages: ['capacity=35228 kWp exceeds rooftop envelope [0.05, 2000]'],
      warnings: ['That roof area (234,854 m²) is larger than a typical building — please confirm it.'],
    };
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(huge) }),
    );
    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('cv-preset-0').click();
    await page.getByTestId('cv-area').fill('234854');
    // The inline guardrail fires on the input itself (before Estimate).
    await expect(page.getByTestId('cv-area-warning')).toBeVisible();
    await page.getByTestId('consumer-estimate-btn').click();
    // The result surfaces both the friendly warning and the hard sanity failure.
    await expect(page.getByTestId('cv-warnings')).toBeVisible();
    await expect(page.getByTestId('cv-sanity-fail')).toBeVisible();
    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: map pin is reverse-geocoded to a place name', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/geocode/reverse**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ label: 'Giza, Egypt', lat: 30, lon: 31 }) }),
    );
    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    const map = page.getByTestId('cv-map');
    await expect(map).toBeVisible();
    await page.waitForTimeout(700);
    await clickMapFrac(map, 0.5, 0.5);
    await expect(page.getByTestId('cv-location-status')).toContainText('Giza, Egypt');
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: orientation-vs-optimal + P50/P90 render from a real roof', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    const withDetail = {
      ...ROOFTOP_RESULT,
      production_detail: {
        surface_tilt: 0,
        surface_azimuth: 180,
        optimal_tilt: 31,
        optimal_azimuth: 180,
        optimal_specific_yield_kwh_kwp_yr: 1850,
        orientation_ratio: 0.88,
        shading_pct: 3,
        p50_specific_yield_kwh_kwp_yr: 1800,
        p90_specific_yield_kwh_kwp_yr: 1650,
        interannual_note: 'P90 from PVGIS interannual variability (SD_y/E_y = 6.0%).',
      },
    };
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(withDetail) }),
    );
    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('cv-preset-0').click();
    // Open the orientation panel, then pick a non-optimal orientation (human affordance).
    await page.getByText('Roof orientation & shading').click();
    await page.getByTestId('cv-orientation').selectOption('flat');
    await page.getByTestId('consumer-estimate-btn').click();
    await expect(page.getByTestId('cv-orientation-detail')).toBeVisible();
    await expect(page.getByTestId('cv-orientation-ratio')).toContainText('% of optimal');
    await expect(page.getByTestId('cv-p50p90')).toContainText('P90');
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: pick load profile + export policy → self-consumption panel renders', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    const detailed = {
      ...ROOFTOP_RESULT,
      energy: {
        ...ROOFTOP_RESULT.energy,
        self_consumed_kwh: 9000,
        exported_kwh: 18000,
        grid_import_kwh: 3000,
        self_consumption_ratio: 0.33,
        self_sufficiency: 0.75,
        dispatch_policy: 'self_consumption_diurnal_evening',
      },
    };
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detailed) }),
    );
    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('cv-preset-0').click();
    await page.getByTestId('cv-load-profile').selectOption('daytime');
    await page.getByTestId('cv-dispatch-policy').selectOption('self_consumption');
    await page.getByTestId('consumer-estimate-btn').click();
    await expect(page.getByTestId('cv-selfconsumption')).toBeVisible();
    await expect(page.getByTestId('cv-selfconsumption')).toContainText('Exported to grid');
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('consumer: IRR + CO2 render when provided; CO2 says "not available" otherwise', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    const withEconCo2 = {
      ...ROOFTOP_RESULT,
      economics: {
        ...ROOFTOP_RESULT.economics,
        install_cost_usd: 45000,
        net_install_cost_usd: 45000,
        annual_savings_usd: 3500,
        simple_payback_years: 12.9,
        npv_usd: 8000,
        irr_pct: 7.4,
        lifetime_savings_usd: 70000,
        cashflow: [{ year: 0, annual_cash_usd: -45000, cumulative_usd: -45000 }],
        unverified_inputs: [],
      },
      co2: {
        grid_factor_g_per_kwh: 400,
        annual_kg: 9600,
        lifetime_kg: 240000,
        basis: 'average grid emissions (user-provided factor)',
        note: 'Using 400 gCO2/kWh (your input). Average, not marginal, emissions.',
      },
    };
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CRITERIA) }),
    );
    await page.route('**/consumer/rooftop', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(withEconCo2) }),
    );
    await page.goto('/');
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('cv-preset-0').click();
    await page.getByTestId('cv-grid-co2').fill('400');
    await page.getByTestId('consumer-estimate-btn').click();
    await expect(page.getByTestId('cv-irr')).toContainText('%');
    await expect(page.getByTestId('cv-co2')).toContainText('CO₂');
    await expect(page.getByTestId('cv-co2')).toContainText('per year');
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('utility: click Draw on Map → trace a polygon → run a job', async ({ page }) => {
    const errs = trackErrors(page);
    await blockTiles(page);
    let jobCalls = 0;
    await page.route('**/criteria', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(UTIL_CRITERIA) }),
    );
    await page.route('**/analyze', (r) =>
      r.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'j1', status: 'queued' }) }),
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
          n_sites: done ? 1 : 0,
          skipped_sources: [],
        }),
      });
    });
    await page.route('**/jobs/j1/sites.geojson', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(UTIL_SITES) }),
    );
    await page.route('**/jobs/j1/layers/lsi.bounds', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ west: 27, south: 31, east: 28, north: 31.5 }) }),
    );

    await page.goto('/');
    await page.getByTestId('mode-utility').click();
    const map = page.getByTestId('map-container');
    await expect(map).toBeVisible();
    await page.waitForTimeout(700);

    // Reproduce the original bug surface: click the REAL draw button, then draw.
    await page.getByTestId('draw-aoi-btn').click();
    await expect(page.getByTestId('aoi-draw-hint')).toBeVisible();
    await clickMapFrac(map, 0.4, 0.4);
    await clickMapFrac(map, 0.6, 0.4);
    await clickMapFrac(map, 0.6, 0.6);
    await clickMapFrac(map, 0.4, 0.6);
    await page.getByTestId('finish-aoi-btn').click();

    // The drawn polygon must register as an AOI (area feedback appears)...
    await expect(page.getByTestId('aoi-area-feedback')).toBeVisible();
    // ...and a real job must run to completion from it.
    const runBtn = page.getByTestId('run-analysis-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();
    await expect(page.locator('.progress-status-done')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('ranking-table')).toBeVisible({ timeout: 10000 });

    expect(await rootLen(page)).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });
});
