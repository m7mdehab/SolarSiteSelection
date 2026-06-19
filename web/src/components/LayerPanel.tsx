import { useAppContext } from '../context/AppContext';
import '../styles/LayerPanel.css';

const LAYER_LABELS: Record<string, string> = {
  lsi: 'Land Suitability Index',
  slope: 'Slope',
  ghi: 'Global Horizontal Irradiance',
  aspect: 'Aspect',
  landcover: 'Land Cover',
  distance_road: 'Distance to Road',
  distance_grid: 'Distance to Grid',
  exclusion: 'Exclusion Zones',
};

const LSI_CLASSES = [
  { id: 1, label: 'Very Low', color: '#8B0000' },
  { id: 2, label: 'Low', color: '#FF6600' },
  { id: 3, label: 'Moderate', color: '#FFD700' },
  { id: 4, label: 'High', color: '#90EE90' },
  { id: 5, label: 'Very High', color: '#006400' },
];

interface LayerPanelProps {
  lsiClassesFromApi?: { id: number; label: string; description: string }[];
}

export function LayerPanel({ lsiClassesFromApi }: LayerPanelProps) {
  const { state, dispatch } = useAppContext();

  if (state.availableLayers.length === 0) return null;

  const lsiClasses = lsiClassesFromApi
    ? lsiClassesFromApi.map((c, i) => ({
        id: c.id,
        label: c.label,
        color: LSI_CLASSES[i]?.color ?? '#999',
      }))
    : LSI_CLASSES;

  return (
    <div className="layer-panel" data-testid="layer-panel">
      <div className="layer-panel-title">Layers</div>
      {state.availableLayers.map((layer) => (
        <label key={layer} className="layer-item">
          <input
            type="checkbox"
            checked={state.visibleLayers.has(layer)}
            onChange={() => dispatch({ type: 'TOGGLE_LAYER', layer })}
            data-testid={`layer-toggle-${layer}`}
          />
          <span className="layer-name">{LAYER_LABELS[layer] ?? layer}</span>
        </label>
      ))}

      {state.availableLayers.includes('lsi') && (
        <div className="lsi-legend" data-testid="lsi-legend">
          <div className="lsi-legend-title">LSI Classes</div>
          {lsiClasses.map((cls) => (
            <div key={cls.id} className="lsi-legend-item">
              <span className="lsi-legend-swatch" style={{ background: cls.color }} />
              <span className="lsi-legend-label">
                {cls.id} – {cls.label}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
