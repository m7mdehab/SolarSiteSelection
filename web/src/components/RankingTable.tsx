import { useAppContext } from '../context/AppContext';
import type { SiteFeature } from '../types/api';
import { fmt } from '../util/format';
import '../styles/RankingTable.css';

export function RankingTable() {
  const { state, dispatch } = useAppContext();

  if (!state.sites || state.sites.features.length === 0) return null;

  const sorted = [...state.sites.features].sort(
    (a, b) => a.properties.rank - b.properties.rank
  );

  function handleSelect(site: SiteFeature) {
    dispatch({ type: 'SET_SELECTED_SITE', site });
  }

  return (
    <div className="ranking-table-wrapper" data-testid="ranking-table">
      <div className="ranking-table-title">Site Rankings</div>
      <table className="ranking-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Area km&sup2;</th>
            <th>Mean LSI</th>
            <th>kWh/kWp/yr</th>
            <th>GWh/yr</th>
            <th>LCOE</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((site) => {
            const p = site.properties;
            const isSelected = state.selectedSite?.properties.rank === p.rank;
            return (
              <tr
                key={p.rank}
                className={`ranking-row ${isSelected ? 'ranking-row-selected' : ''}`}
                onClick={() => handleSelect(site)}
                data-testid={`site-row-${p.rank}`}
              >
                <td>{p.rank}</td>
                <td>{fmt(p.area_km2, 2)}</td>
                <td>{fmt(p.mean_lsi, 3)}</td>
                <td data-testid={`site-yield-${p.rank}`}>{fmt(p.kwh_per_kwp_yr, 0)}</td>
                <td>{fmt(p.gwh_per_yr, 2)}</td>
                <td>{fmt(p.lcoe, 4)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
