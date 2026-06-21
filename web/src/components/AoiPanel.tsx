import { useRef } from 'react';
import { useAppContext } from '../context/AppContext';
import { PRESET_AOIS } from '../data/presetAois';
import '../styles/AoiPanel.css';

interface AoiPanelProps {
  onSelectPreset: (id: string) => void;
  onStartDraw: () => void;
  onStopDraw: () => void;
  onFinishDraw: () => void;
  isDrawing: boolean;
}

export function AoiPanel({
  onSelectPreset,
  onStartDraw,
  onStopDraw,
  onFinishDraw,
  isDrawing,
}: AoiPanelProps) {
  const { state } = useAppContext();
  const fileRef = useRef<HTMLInputElement>(null);
  const { dispatch } = useAppContext();

  function handlePresetChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const id = e.target.value;
    if (!id) return;
    onSelectPreset(id);
  }

  function handleFileUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const gj = JSON.parse(ev.target?.result as string);
        let feature: GeoJSON.Feature<GeoJSON.Polygon> | null = null;
        if (gj.type === 'FeatureCollection' && gj.features?.length > 0) {
          feature = gj.features[0] as GeoJSON.Feature<GeoJSON.Polygon>;
        } else if (gj.type === 'Feature') {
          feature = gj as GeoJSON.Feature<GeoJSON.Polygon>;
        } else if (gj.type === 'Polygon') {
          feature = { type: 'Feature', geometry: gj, properties: {} };
        }
        if (feature) {
          dispatch({ type: 'SET_AOI', aoi: feature });
        }
      } catch {
        alert('Invalid GeoJSON file');
      }
    };
    reader.readAsText(file);
    if (fileRef.current) fileRef.current.value = '';
  }

  const area = state.aoiAreaKm2;
  const areaWarning = area !== null && area > 10000;

  return (
    <div className="aoi-panel">
      <div className="aoi-panel-title">Area of Interest</div>
      <select
        className="aoi-preset-select"
        onChange={handlePresetChange}
        defaultValue=""
        data-testid="aoi-preset-select"
      >
        <option value="">Select preset AOI...</option>
        {PRESET_AOIS.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>

      <div className="aoi-actions">
        {!isDrawing ? (
          <button className="aoi-btn" onClick={onStartDraw} data-testid="draw-aoi-btn">
            Draw on Map
          </button>
        ) : (
          <>
            <button
              className="aoi-btn aoi-btn-primary"
              onClick={onFinishDraw}
              data-testid="finish-aoi-btn"
            >
              Finish polygon
            </button>
            <button className="aoi-btn aoi-btn-active" onClick={onStopDraw} data-testid="cancel-aoi-btn">
              Cancel
            </button>
          </>
        )}
        <button
          className="aoi-btn"
          onClick={() => fileRef.current?.click()}
          data-testid="upload-aoi-btn"
        >
          Upload GeoJSON
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".json,.geojson"
          style={{ display: 'none' }}
          onChange={handleFileUpload}
        />
      </div>

      {isDrawing && (
        <div className="aoi-draw-hint" data-testid="aoi-draw-hint">
          Click the map to add corners, then “Finish polygon” (or double-click).
        </div>
      )}

      {area !== null && (
        <div className={`aoi-area-feedback ${areaWarning ? 'aoi-area-warning' : 'aoi-area-ok'}`} data-testid="aoi-area-feedback">
          Area: {area.toFixed(1)} km&sup2;
          {areaWarning && ' - Warning: area exceeds 10,000 km2, analysis may be slow'}
        </div>
      )}
    </div>
  );
}
