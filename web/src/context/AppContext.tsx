import React, { createContext, useContext, useReducer, type ReactNode } from 'react';
import type { CriteriaResponse, JobResponse, SitesGeoJSON, SiteFeature } from '../types/api';

export interface AppState {
  // AOI
  aoi: GeoJSON.Feature<GeoJSON.Polygon> | null;
  aoiAreaKm2: number | null;

  // Criteria
  criteria: CriteriaResponse | null;
  weightOverrides: Record<string, number>;

  // Job
  jobId: string | null;
  job: JobResponse | null;

  // Results
  sites: SitesGeoJSON | null;
  selectedSite: SiteFeature | null;

  // Layers
  availableLayers: string[];
  visibleLayers: Set<string>;

  // UI
  isRunning: boolean;
  expertMode: boolean;
  error: string | null;
}

type Action =
  | { type: 'SET_AOI'; aoi: GeoJSON.Feature<GeoJSON.Polygon> | null }
  | { type: 'SET_AOI_AREA'; areakm2: number | null }
  | { type: 'SET_CRITERIA'; criteria: CriteriaResponse }
  | { type: 'SET_WEIGHT_OVERRIDE'; key: string; weight: number }
  | { type: 'RESET_WEIGHTS' }
  | { type: 'SET_JOB_ID'; jobId: string }
  | { type: 'SET_JOB'; job: JobResponse }
  | { type: 'SET_SITES'; sites: SitesGeoJSON }
  | { type: 'SET_SELECTED_SITE'; site: SiteFeature | null }
  | { type: 'SET_AVAILABLE_LAYERS'; layers: string[] }
  | { type: 'TOGGLE_LAYER'; layer: string }
  | { type: 'SET_IS_RUNNING'; isRunning: boolean }
  | { type: 'SET_EXPERT_MODE'; expertMode: boolean }
  | { type: 'SET_ERROR'; error: string | null }
  | { type: 'RESET_JOB' };

const initialState: AppState = {
  aoi: null,
  aoiAreaKm2: null,
  criteria: null,
  weightOverrides: {},
  jobId: null,
  job: null,
  sites: null,
  selectedSite: null,
  availableLayers: [],
  visibleLayers: new Set(),
  isRunning: false,
  expertMode: false,
  error: null,
};

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case 'SET_AOI':
      return { ...state, aoi: action.aoi };
    case 'SET_AOI_AREA':
      return { ...state, aoiAreaKm2: action.areakm2 };
    case 'SET_CRITERIA':
      return { ...state, criteria: action.criteria };
    case 'SET_WEIGHT_OVERRIDE':
      return {
        ...state,
        weightOverrides: { ...state.weightOverrides, [action.key]: action.weight },
      };
    case 'RESET_WEIGHTS':
      return { ...state, weightOverrides: {} };
    case 'SET_JOB_ID':
      return { ...state, jobId: action.jobId };
    case 'SET_JOB':
      return { ...state, job: action.job };
    case 'SET_SITES':
      return { ...state, sites: action.sites };
    case 'SET_SELECTED_SITE':
      return { ...state, selectedSite: action.site };
    case 'SET_AVAILABLE_LAYERS':
      return {
        ...state,
        availableLayers: action.layers,
        visibleLayers: new Set(action.layers.slice(0, 1)),
      };
    case 'TOGGLE_LAYER': {
      const next = new Set(state.visibleLayers);
      if (next.has(action.layer)) {
        next.delete(action.layer);
      } else {
        next.add(action.layer);
      }
      return { ...state, visibleLayers: next };
    }
    case 'SET_IS_RUNNING':
      return { ...state, isRunning: action.isRunning };
    case 'SET_EXPERT_MODE':
      return { ...state, expertMode: action.expertMode };
    case 'SET_ERROR':
      return { ...state, error: action.error };
    case 'RESET_JOB':
      return {
        ...state,
        jobId: null,
        job: null,
        sites: null,
        selectedSite: null,
        availableLayers: [],
        visibleLayers: new Set(),
        isRunning: false,
        error: null,
      };
    default:
      return state;
  }
}

interface AppContextValue {
  state: AppState;
  dispatch: React.Dispatch<Action>;
}

const AppContext = createContext<AppContextValue | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return <AppContext.Provider value={{ state, dispatch }}>{children}</AppContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAppContext(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be used within AppProvider');
  return ctx;
}
