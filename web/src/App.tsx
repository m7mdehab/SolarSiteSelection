import { useEffect, useRef, useState } from 'react';
import { useAppContext } from './context/AppContext';
import { getCriteria, startAnalysis, getJob, getSites } from './api/client';
import { PRESET_AOIS } from './data/presetAois';
import { Map, type MapHandle } from './components/Map';
import { AoiPanel } from './components/AoiPanel';
import { CriteriaPanel } from './components/CriteriaPanel';
import { ProgressView } from './components/ProgressView';
import { LayerPanel } from './components/LayerPanel';
import { RankingTable } from './components/RankingTable';
import { DownloadButtons } from './components/DownloadButtons';
import { SitePopup } from './components/SitePopup';
import type { AppState } from './context/AppContext';
import './styles/App.css';

const POLL_INTERVAL_MS = 2000;

// Compute polygon area in km2 (shoelace formula, spherical approximation)
function computeAreaKm2(coords: [number, number][]): number {
  const R = 6371; // km
  const n = coords.length;
  if (n < 3) return 0;
  let area = 0;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    const xi = (coords[i][0] * Math.PI) / 180;
    const yi = (coords[i][1] * Math.PI) / 180;
    const xj = (coords[j][0] * Math.PI) / 180;
    const yj = (coords[j][1] * Math.PI) / 180;
    area += xi * Math.sin(yj);
    area -= xj * Math.sin(yi);
  }
  return Math.abs(area / 2) * R * R;
}

type DispatchFn = React.Dispatch<Parameters<typeof import('./context/AppContext').AppProvider>[0]>;

// Module-level poll helper – accepts dispatch and a timer ref so it can schedule itself.
// Using a module-level function avoids the react-hooks/immutability restriction on
// self-referencing useCallback variables.
function schedulePoll(
  jobId: string,
  timerRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>,
  dispatch: React.Dispatch<{ type: string; [k: string]: unknown }>
): void {
  timerRef.current = setTimeout(() => {
    void pollOnce(jobId, timerRef, dispatch);
  }, POLL_INTERVAL_MS);
}

async function pollOnce(
  jobId: string,
  timerRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>,
  dispatch: React.Dispatch<{ type: string; [k: string]: unknown }>
): Promise<void> {
  try {
    const job = await getJob(jobId);
    dispatch({ type: 'SET_JOB', job } as never);

    if (job.status === 'done') {
      dispatch({ type: 'SET_IS_RUNNING', isRunning: false } as never);
      try {
        const sites = await getSites(jobId);
        dispatch({ type: 'SET_SITES', sites } as never);
      } catch {
        // ignore
      }
      dispatch({ type: 'SET_AVAILABLE_LAYERS', layers: ['lsi'] } as never);
      return;
    }

    if (job.status === 'error') {
      dispatch({ type: 'SET_IS_RUNNING', isRunning: false } as never);
      dispatch({ type: 'SET_ERROR', error: job.error ?? 'Analysis failed' } as never);
      return;
    }

    schedulePoll(jobId, timerRef, dispatch);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    dispatch({ type: 'SET_IS_RUNNING', isRunning: false } as never);
    dispatch({ type: 'SET_ERROR', error: `Polling error: ${message}` } as never);
  }
}

// Satisfy unused import for AppState type (used by context only)
type _AppStateRef = AppState;
const _unused: _AppStateRef | DispatchFn | null = null;
void _unused;

export function App() {
  const { state, dispatch } = useAppContext();
  const mapRef = useRef<MapHandle>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isDrawing, setIsDrawing] = useState(false);

  // Fetch criteria on mount
  useEffect(() => {
    getCriteria()
      .then((c) => dispatch({ type: 'SET_CRITERIA', criteria: c }))
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        dispatch({ type: 'SET_ERROR', error: `Failed to load criteria: ${message}` });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Compute area when AOI changes
  useEffect(() => {
    if (!state.aoi) {
      dispatch({ type: 'SET_AOI_AREA', areakm2: null });
      return;
    }
    const coords = state.aoi.geometry.coordinates[0] as [number, number][];
    const area = computeAreaKm2(coords);
    dispatch({ type: 'SET_AOI_AREA', areakm2: area });
  }, [state.aoi, dispatch]);

  function handlePresetSelect(id: string) {
    const preset = PRESET_AOIS.find((p) => p.id === id);
    if (!preset) return;
    dispatch({ type: 'SET_AOI', aoi: preset.geojson });
    mapRef.current?.fitAoi(preset.geojson);
  }

  function handleAoiDrawn(feature: GeoJSON.Feature<GeoJSON.Polygon>) {
    dispatch({ type: 'SET_AOI', aoi: feature });
    setIsDrawing(false);
    mapRef.current?.fitAoi(feature);
  }

  function handleStartDraw() {
    setIsDrawing(true);
    mapRef.current?.startDraw();
  }

  function handleStopDraw() {
    setIsDrawing(false);
    mapRef.current?.stopDraw();
  }

  async function handleRunAnalysis() {
    if (!state.aoi) return;
    dispatch({ type: 'RESET_JOB' });
    dispatch({ type: 'SET_IS_RUNNING', isRunning: true });
    dispatch({ type: 'SET_ERROR', error: null });

    try {
      const res = await startAnalysis({
        aoi: state.aoi,
        weight_overrides:
          Object.keys(state.weightOverrides).length > 0 ? state.weightOverrides : undefined,
      });
      dispatch({ type: 'SET_JOB_ID', jobId: res.job_id });
      void pollOnce(res.job_id, pollTimerRef, dispatch as React.Dispatch<{ type: string; [k: string]: unknown }>);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      dispatch({ type: 'SET_IS_RUNNING', isRunning: false });
      dispatch({ type: 'SET_ERROR', error: `Failed to start analysis: ${message}` });
    }
  }

  // Cleanup poll timer on unmount
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  const canRun = state.aoi !== null && !state.isRunning;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <span className="app-header-title">Solar Site Selection</span>
          <span className="app-header-subtitle">Geospatial Analysis Tool</span>
        </div>
      </header>

      <div className="app-body">
        {/* Left Sidebar */}
        <aside className="app-sidebar">
          <div className="app-sidebar-scroll">
            <AoiPanel
              onSelectPreset={handlePresetSelect}
              onStartDraw={handleStartDraw}
              onStopDraw={handleStopDraw}
              isDrawing={isDrawing}
            />
            <CriteriaPanel />
            {state.error && <div className="app-error-banner">{state.error}</div>}
          </div>
          <div className="app-sidebar-bottom">
            <button
              className="run-button"
              onClick={() => void handleRunAnalysis()}
              disabled={!canRun}
              data-testid="run-analysis-btn"
            >
              {state.isRunning ? 'Running Analysis...' : 'Run Analysis'}
            </button>
          </div>
        </aside>

        {/* Map */}
        <main className="app-map-area">
          <Map ref={mapRef} onAoiDrawn={handleAoiDrawn} />
          {state.job && (
            <div className="app-map-overlay">
              <LayerPanel lsiClassesFromApi={state.criteria?.lsi_classes} />
            </div>
          )}
        </main>

        {/* Right Panel */}
        {(state.job || state.sites) && (
          <aside className="app-right-panel">
            <div className="app-right-scroll">
              {state.job && <ProgressView job={state.job} />}
              {state.selectedSite && (
                <SitePopup
                  props={state.selectedSite.properties}
                  onClose={() => dispatch({ type: 'SET_SELECTED_SITE', site: null })}
                />
              )}
              {state.sites && <RankingTable />}
              {state.job?.status === 'done' && <DownloadButtons />}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
