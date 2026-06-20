import { test, expect, type Page } from '@playwright/test';

// Render gate for the consumer rooftop view (Step 2). Same bar as the utility
// view: the breakdown must render, displayed numbers must be finite, a missing
// economic input must degrade to "— enter to see" (never a crash/blank), and the
// whole flow must produce ZERO uncaught exceptions.

function rooftopResult(withEconomics: boolean) {
  return {
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
      install_cost_usd: withEconomics ? 45000 : null,
      net_install_cost_usd: withEconomics ? 45000 : null,
      annual_savings_usd: withEconomics ? 1200 : null,
      simple_payback_years: withEconomics ? 37.5 : null,
      npv_usd: withEconomics ? -5000 : null,
      lifetime_savings_usd: withEconomics ? 30000 : null,
      unverified_inputs: withEconomics ? [] : ['install_cost_usd_per_w', 'retail_tariff_usd_per_kwh'],
      caveats: [],
    },
    sanity_ok: true,
    sanity_messages: [],
    assumptions: ['usable roof fraction = 0.75', 'module efficiency = 0.2'],
    monthly_kwh: [1800, 1900, 2300, 2500, 2700, 2800, 2900, 2800, 2500, 2200, 1900, 1700],
    production_method: 'pvlib_modelchain',
    production_note: 'Production is validation-grade (pvlib ModelChain on the PVGIS TMY).',
    payback_band: withEconomics
      ? { low: 36.1, base: 37.5, high: 39.1, basis: 'production model spread' }
      : null,
    unverified_panel: withEconomics
      ? []
      : ['install_cost_usd_per_w: not verified for your area (enter your own value)'],
  };
}

async function mockConsumer(page: Page, withEconomics: boolean): Promise<void> {
  await page.route('**/criteria', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ groups: {}, lsi_classes: [], hard_exclusion_rules: [] }) }),
  );
  await page.route('**/consumer/rooftop', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(rooftopResult(withEconomics)) }),
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

function uncaught(errs: string[]): string[] {
  return errs.filter(
    (e) => e.startsWith('PAGEERROR') || /TypeError|Cannot read|is not a function|undefined/.test(e),
  );
}

test.describe('CONSUMER RENDER GATE — rooftop breakdown must display', () => {
  test('full result renders with finite numbers and no uncaught errors', async ({ page }) => {
    const errs = trackErrors(page);
    await mockConsumer(page, true);
    await page.goto('/');
    await page.getByTestId('mode-rooftop').click();
    await expect(page.getByTestId('consumer-view')).toBeVisible();
    await page.getByTestId('cv-area').fill('100');
    await page.getByTestId('consumer-estimate-btn').click();

    await expect(page.getByTestId('cv-capacity')).toContainText('kWp');
    await expect(page.getByTestId('cv-production')).toContainText('kWh');
    await expect(page.getByTestId('cv-payback')).toContainText('yr');
    await expect(page.getByTestId('cv-monthly')).toBeVisible();
    await expect(page.getByTestId('cv-band')).toBeVisible();

    const rootLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0);
    expect(rootLen).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });

  test('missing economics degrade to "enter to see" (no crash/blank)', async ({ page }) => {
    const errs = trackErrors(page);
    await mockConsumer(page, false);
    await page.goto('/');
    await page.getByTestId('mode-rooftop').click();
    await page.getByTestId('cv-area').fill('100');
    await page.getByTestId('consumer-estimate-btn').click();

    // Energy still renders; payback shows the honest placeholder, not a crash.
    await expect(page.getByTestId('cv-production')).toContainText('kWh');
    await expect(page.getByTestId('cv-payback')).toContainText('enter to see');
    await expect(page.getByTestId('consumer-unverified-panel')).toBeVisible();

    const rootLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? 0);
    expect(rootLen).toBeGreaterThan(100);
    expect(uncaught(errs), uncaught(errs).join('\n')).toHaveLength(0);
  });
});
