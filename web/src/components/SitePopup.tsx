import type { SiteProperties } from '../types/api';
import { fmt } from '../util/format';
import '../styles/SitePopup.css';

interface SitePopupProps {
  props: SiteProperties;
  onClose?: () => void;
  // Namespaces the data-testids so the side-panel and map-popup instances of
  // this component never collide (Playwright strict mode). Defaults to "site"
  // (the canonical instance the acceptance test targets).
  testIdPrefix?: string;
}

export function SitePopup({ props, onClose, testIdPrefix = 'site' }: SitePopupProps) {
  return (
    <div className="site-popup" data-testid={`${testIdPrefix}-popup`}>
      <div className="site-popup-header">
        <span className="site-popup-rank">Site #{props.rank}</span>
        {onClose && (
          <button className="site-popup-close" onClick={onClose} aria-label="Close">
            x
          </button>
        )}
      </div>
      <div className="site-popup-body">
        <div className="site-popup-row">
          <span className="site-popup-label">Area</span>
          <span className="site-popup-value" data-testid={`${testIdPrefix}-area`}>
            {fmt(props.area_km2, 2)} km&sup2;
          </span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Mean LSI</span>
          <span className="site-popup-value">{fmt(props.mean_lsi, 3)}</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Yield</span>
          <span className="site-popup-value" data-testid={`${testIdPrefix}-yield`}>
            {fmt(props.kwh_per_kwp_yr, 0)} kWh/kWp/yr
          </span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Generation</span>
          <span className="site-popup-value">{fmt(props.gwh_per_yr, 2)} GWh/yr</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">LCOE</span>
          <span className="site-popup-value">{fmt(props.lcoe, 4)} USD/kWh</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Centroid</span>
          <span className="site-popup-value">
            {fmt(props.centroid_lat, 4)}, {fmt(props.centroid_lon, 4)}
          </span>
        </div>
      </div>
    </div>
  );
}
