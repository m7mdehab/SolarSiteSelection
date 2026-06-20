import { useState } from 'react';
import { analyzeRooftop } from '../api/client';
import type { RooftopRequest, RooftopResult } from '../types/api';
import { fmt } from '../util/format';
import '../styles/ConsumerView.css';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const PRESET_LOCATIONS = [
  { label: 'NW Egypt coast', lat: 31.1, lon: 27.5 },
  { label: 'Aswan (desert)', lat: 24.09, lon: 32.9 },
  { label: 'Munich', lat: 48.14, lon: 11.58 },
  { label: 'Cape Town', lat: -33.93, lon: 18.42 },
];

// Money figure or an honest placeholder — never a fabricated value.
function money(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? `$${fmt(v, 0)}` : '— enter to see';
}

export function ConsumerView() {
  const [lat, setLat] = useState('31.1');
  const [lon, setLon] = useState('27.5');
  const [areaM2, setAreaM2] = useState('100');
  const [usable, setUsable] = useState('0.75');
  const [efficiency, setEfficiency] = useState('0.20');
  const [annualKwh, setAnnualKwh] = useState('6000');
  // Economics — all optional, user-entered (no defaults invented).
  const [costPerW, setCostPerW] = useState('');
  const [tariff, setTariff] = useState('');
  const [exportRate, setExportRate] = useState('');
  const [incentive, setIncentive] = useState('');

  const [result, setResult] = useState<RooftopResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function numOrUndef(s: string): number | undefined {
    const v = parseFloat(s);
    return Number.isFinite(v) ? v : undefined;
  }

  async function handleEstimate() {
    setBusy(true);
    setError(null);
    try {
      const econ = {
        install_cost_usd_per_w: numOrUndef(costPerW) ?? null,
        retail_tariff_usd_per_kwh: numOrUndef(tariff) ?? null,
        export_rate_usd_per_kwh: numOrUndef(exportRate) ?? null,
        incentive_usd: numOrUndef(incentive) ?? null,
      };
      const req: RooftopRequest = {
        roof: {
          area_m2: numOrUndef(areaM2) ?? 100,
          usable_fraction: numOrUndef(usable),
          module_efficiency: numOrUndef(efficiency),
        },
        latitude: numOrUndef(lat),
        longitude: numOrUndef(lon),
        consumption: { annual_kwh: numOrUndef(annualKwh) ?? null },
        economics: econ,
      };
      const res = await analyzeRooftop(req);
      setResult(res);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  const maxMonthly = result?.monthly_kwh ? Math.max(...result.monthly_kwh, 1) : 1;

  return (
    <div className="consumer-view" data-testid="consumer-view">
      <div className="consumer-grid">
        {/* ---- Inputs ---- */}
        <section className="consumer-card">
          <h3>Your rooftop</h3>

          <label className="cv-label">Location</label>
          <div className="cv-presets">
            {PRESET_LOCATIONS.map((p) => (
              <button
                key={p.label}
                type="button"
                className="cv-preset-btn"
                onClick={() => {
                  setLat(String(p.lat));
                  setLon(String(p.lon));
                }}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="cv-row">
            <input aria-label="latitude" value={lat} onChange={(e) => setLat(e.target.value)} />
            <input aria-label="longitude" value={lon} onChange={(e) => setLon(e.target.value)} />
          </div>

          <label className="cv-label">Roof area (m²)</label>
          <input
            data-testid="cv-area"
            value={areaM2}
            onChange={(e) => setAreaM2(e.target.value)}
          />
          <div className="cv-row">
            <div>
              <label className="cv-label">Usable fraction</label>
              <input value={usable} onChange={(e) => setUsable(e.target.value)} />
            </div>
            <div>
              <label className="cv-label">Module efficiency</label>
              <input value={efficiency} onChange={(e) => setEfficiency(e.target.value)} />
            </div>
          </div>

          <label className="cv-label">Annual electricity use (kWh)</label>
          <input value={annualKwh} onChange={(e) => setAnnualKwh(e.target.value)} />

          <h3 className="cv-econ-head">Economics (optional — your own numbers)</h3>
          <p className="cv-hint">
            Leave blank if unknown — money figures stay blank rather than guessed.
          </p>
          <div className="cv-row">
            <div>
              <label className="cv-label">Install cost ($/W)</label>
              <input value={costPerW} onChange={(e) => setCostPerW(e.target.value)} placeholder="e.g. 3.0" />
            </div>
            <div>
              <label className="cv-label">Your rate ($/kWh)</label>
              <input value={tariff} onChange={(e) => setTariff(e.target.value)} placeholder="from your bill" />
            </div>
          </div>
          <div className="cv-row">
            <div>
              <label className="cv-label">Export rate ($/kWh)</label>
              <input value={exportRate} onChange={(e) => setExportRate(e.target.value)} placeholder="policy" />
            </div>
            <div>
              <label className="cv-label">Incentive ($)</label>
              <input value={incentive} onChange={(e) => setIncentive(e.target.value)} placeholder="0" />
            </div>
          </div>

          <button
            className="cv-estimate-btn"
            data-testid="consumer-estimate-btn"
            onClick={() => void handleEstimate()}
            disabled={busy}
          >
            {busy ? 'Estimating…' : 'Estimate'}
          </button>
          {error && (
            <div className="cv-error" data-testid="consumer-error">
              {error}
            </div>
          )}
        </section>

        {/* ---- Outputs ---- */}
        <section className="consumer-card" data-testid="consumer-output">
          <h3>Technical breakdown</h3>
          {!result && <p className="cv-hint">Enter your details and press Estimate.</p>}
          {result && (
            <>
              <div className="cv-metrics">
                <div className="cv-metric">
                  <span className="cv-metric-label">System size</span>
                  <span className="cv-metric-value" data-testid="cv-capacity">
                    {fmt(result.energy.capacity_kwp, 1)} kWp
                  </span>
                </div>
                <div className="cv-metric">
                  <span className="cv-metric-label">Annual production</span>
                  <span className="cv-metric-value" data-testid="cv-production">
                    {fmt(result.energy.annual_production_kwh, 0)} kWh
                  </span>
                </div>
                <div className="cv-metric">
                  <span className="cv-metric-label">Specific yield</span>
                  <span className="cv-metric-value">
                    {fmt(result.energy.specific_yield_kwh_kwp_yr, 0)} kWh/kWp
                  </span>
                </div>
                <div className="cv-metric">
                  <span className="cv-metric-label">Self-sufficiency</span>
                  <span className="cv-metric-value">
                    {fmt(result.energy.self_sufficiency * 100, 0)}%
                  </span>
                </div>
              </div>

              <div
                className={`cv-method ${result.production_method === 'pvlib_modelchain' ? 'cv-method-ok' : ''}`}
                data-testid="cv-method"
              >
                {result.production_method === 'pvlib_modelchain'
                  ? 'Production: validation-grade (pvlib + PVGIS)'
                  : 'Production: caller-supplied estimate'}
              </div>
              {result.production_note && <p className="cv-note">{result.production_note}</p>}

              {result.monthly_kwh && (
                <div className="cv-monthly" data-testid="cv-monthly">
                  <div className="cv-sub">Monthly production (kWh)</div>
                  <div className="cv-bars">
                    {result.monthly_kwh.map((m, i) => (
                      <div className="cv-bar-col" key={MONTHS[i]} title={`${MONTHS[i]}: ${fmt(m, 0)} kWh`}>
                        <div className="cv-bar" style={{ height: `${(m / maxMonthly) * 60}px` }} />
                        <span className="cv-bar-label">{MONTHS[i][0]}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="cv-econ">
                <div className="cv-sub">Economics</div>
                <table className="cv-econ-table">
                  <tbody>
                    <tr>
                      <td>Install cost</td>
                      <td>{money(result.economics.install_cost_usd)}</td>
                    </tr>
                    <tr>
                      <td>Annual savings</td>
                      <td>{money(result.economics.annual_savings_usd)}</td>
                    </tr>
                    <tr>
                      <td>Simple payback</td>
                      <td data-testid="cv-payback">
                        {typeof result.economics.simple_payback_years === 'number'
                          ? `${fmt(result.economics.simple_payback_years, 1)} yr`
                          : '— enter to see'}
                      </td>
                    </tr>
                    <tr>
                      <td>NPV</td>
                      <td>{money(result.economics.npv_usd)}</td>
                    </tr>
                  </tbody>
                </table>
                {result.payback_band && (
                  <p className="cv-band" data-testid="cv-band">
                    Payback range {fmt(result.payback_band.low, 1)}–{fmt(result.payback_band.high, 1)} yr
                    <span className="cv-band-basis"> ({result.payback_band.basis})</span>
                  </p>
                )}
              </div>

              {result.unverified_panel.length > 0 && (
                <div className="cv-unverified" data-testid="consumer-unverified-panel">
                  <div className="cv-sub">What we can't verify for your area</div>
                  <ul>
                    {result.unverified_panel.map((u, i) => (
                      <li key={i}>{u}</li>
                    ))}
                  </ul>
                </div>
              )}

              {result.assumptions.length > 0 && (
                <details className="cv-assumptions">
                  <summary>Assumptions ledger</summary>
                  <ul>
                    {result.assumptions.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
