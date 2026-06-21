import { useRef, useState } from 'react';
import { analyzeRooftop } from '../api/client';
import type { RooftopRequest, RooftopResult } from '../types/api';
import { fmt } from '../util/format';
import { GeocoderSearch } from './GeocoderSearch';
import { LocationMap, type LocationMapHandle } from './LocationMap';
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
  const mapRef = useRef<LocationMapHandle>(null);
  const [lat, setLat] = useState<number | null>(null);
  const [lon, setLon] = useState<number | null>(null);
  const [placeLabel, setPlaceLabel] = useState<string>('');
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [areaM2, setAreaM2] = useState('100');
  const [areaFromDraw, setAreaFromDraw] = useState(false);
  const [drawingRoof, setDrawingRoof] = useState(false);
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

  function setLocation(newLat: number, newLon: number, label: string) {
    setLat(newLat);
    setLon(newLon);
    setPlaceLabel(label);
    mapRef.current?.flyTo(newLat, newLon);
  }

  // Map click (not drawing a roof) drops/moves the pin.
  function handlePick(pLat: number, pLon: number) {
    setLat(pLat);
    setLon(pLon);
    setPlaceLabel(`Pinned: ${pLat.toFixed(4)}, ${pLon.toFixed(4)}`);
  }

  function handleRoofDrawn(area: number) {
    setAreaM2(area.toFixed(1));
    setAreaFromDraw(true);
    setDrawingRoof(false);
  }

  async function handleEstimate() {
    if (lat == null || lon == null) {
      setError('Set your location first — search a place, click the map, or pick an example.');
      return;
    }
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
        latitude: lat,
        longitude: lon,
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
  const hasLocation = lat != null && lon != null;

  return (
    <div className="consumer-view" data-testid="consumer-view">
      <div className="consumer-grid">
        {/* ---- Inputs ---- */}
        <section className="consumer-card">
          <h3>Where do you live?</h3>
          <p className="cv-hint">
            Search for your town or address, or click the map to drop a pin. Your location
            sets the validation-grade solar resource for the estimate.
          </p>

          <GeocoderSearch onSelect={setLocation} />

          <LocationMap
            ref={mapRef}
            lat={lat}
            lon={lon}
            drawingRoof={drawingRoof}
            onPick={handlePick}
            onRoofDrawn={handleRoofDrawn}
          />

          <div className="cv-location-status" data-testid="cv-location-status">
            {hasLocation ? placeLabel || `${lat?.toFixed(4)}, ${lon?.toFixed(4)}` : 'No location set yet.'}
          </div>

          <div className="cv-presets">
            <span className="cv-presets-label">Quick examples:</span>
            {PRESET_LOCATIONS.map((p, i) => (
              <button
                key={p.label}
                type="button"
                className="cv-preset-btn"
                data-testid={`cv-preset-${i}`}
                onClick={() => setLocation(p.lat, p.lon, p.label)}
              >
                {p.label}
              </button>
            ))}
          </div>

          <button
            type="button"
            className="cv-advanced-toggle"
            onClick={() => setShowAdvanced((s) => !s)}
          >
            {showAdvanced ? '▾ Hide coordinates' : '▸ Enter coordinates manually'}
          </button>
          {showAdvanced && (
            <div className="cv-row">
              <input
                aria-label="latitude"
                placeholder="latitude"
                value={lat ?? ''}
                onChange={(e) => setLat(numOrUndef(e.target.value) ?? null)}
              />
              <input
                aria-label="longitude"
                placeholder="longitude"
                value={lon ?? ''}
                onChange={(e) => setLon(numOrUndef(e.target.value) ?? null)}
              />
            </div>
          )}

          <h3 className="cv-section-head">Your roof</h3>
          <label className="cv-label">Roof area (m²)</label>
          <input
            data-testid="cv-area"
            value={areaM2}
            onChange={(e) => {
              setAreaM2(e.target.value);
              setAreaFromDraw(false);
            }}
          />
          {!drawingRoof ? (
            <button
              type="button"
              className="cv-draw-roof-btn"
              data-testid="cv-draw-roof-btn"
              onClick={() => setDrawingRoof(true)}
            >
              ✏ Draw roof on map
            </button>
          ) : (
            <div className="cv-draw-actions">
              <span className="cv-draw-hint" data-testid="cv-roof-hint">
                Click the roof corners on the map, then Finish.
              </span>
              <button
                type="button"
                className="cv-draw-roof-btn cv-btn-primary"
                data-testid="cv-finish-roof-btn"
                onClick={() => mapRef.current?.finishRoof()}
              >
                Finish roof
              </button>
              <button type="button" className="cv-draw-roof-btn" onClick={() => setDrawingRoof(false)}>
                Cancel
              </button>
            </div>
          )}
          {areaFromDraw && (
            <div className="cv-area-note" data-testid="cv-area-from-draw">
              Area measured from your drawn roof polygon.
            </div>
          )}

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
          {!result && <p className="cv-hint">Set your location and roof, then press Estimate.</p>}
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
