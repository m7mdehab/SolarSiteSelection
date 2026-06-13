import { useState } from 'react';
import { useAppContext } from '../context/AppContext';
import { AhpEditor } from './AhpEditor';
import type { Criterion } from '../types/api';
import '../styles/CriteriaPanel.css';

export function CriteriaPanel() {
  const { state, dispatch } = useAppContext();
  const [activeGroup, setActiveGroup] = useState<string | null>(null);

  if (!state.criteria) {
    return (
      <div className="criteria-panel">
        <div className="criteria-loading">Loading criteria...</div>
      </div>
    );
  }

  const groups = state.criteria.groups;

  return (
    <div className="criteria-panel">
      <div className="criteria-header">
        <span className="criteria-title">Criteria</span>
        <label className="criteria-expert-toggle">
          <input
            type="checkbox"
            checked={state.expertMode}
            onChange={(e) => dispatch({ type: 'SET_EXPERT_MODE', expertMode: e.target.checked })}
          />
          Expert Mode
        </label>
      </div>

      {Object.entries(groups).map(([groupKey, group]) => (
        <div key={groupKey} className="criteria-group">
          <button
            className="criteria-group-header"
            onClick={() => setActiveGroup(activeGroup === groupKey ? null : groupKey)}
            aria-expanded={activeGroup === groupKey}
          >
            <span className="criteria-group-name">{group.name}</span>
            <span className="criteria-group-weight">{(group.weight * 100).toFixed(0)}%</span>
            <span className="criteria-group-chevron">{activeGroup === groupKey ? '▲' : '▼'}</span>
          </button>

          {activeGroup === groupKey && (
            <div className="criteria-group-body">
              {group.criteria.map((c: Criterion) => (
                <div key={c.key} className="criteria-item">
                  <div className="criteria-item-name" title={c.name}>
                    {c.name}
                  </div>
                  <div className="criteria-item-meta">
                    <span className="criteria-item-source">{c.data_source}</span>
                    {c.unit && <span className="criteria-item-unit">{c.unit}</span>}
                  </div>
                  <div className="criteria-item-weight">
                    <label>
                      Global weight: {((c.global_weight) * 100).toFixed(1)}%
                    </label>
                    {state.expertMode && (
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.01}
                        value={state.weightOverrides[c.key] ?? c.local_weight}
                        onChange={(e) =>
                          dispatch({
                            type: 'SET_WEIGHT_OVERRIDE',
                            key: c.key,
                            weight: parseFloat(e.target.value),
                          })
                        }
                      />
                    )}
                  </div>
                </div>
              ))}

              {state.expertMode && group.criteria.length >= 2 && (
                <div className="criteria-ahp-section">
                  <div className="criteria-ahp-title">AHP Pairwise Comparison</div>
                  <AhpEditor
                    criteria={group.criteria}
                    onWeightsChange={(weights) => {
                      Object.entries(weights).forEach(([key, w]) => {
                        dispatch({ type: 'SET_WEIGHT_OVERRIDE', key, weight: w });
                      });
                    }}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
