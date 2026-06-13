import type { SiteProperties } from '../types/api';
import '../styles/SitePopup.css';

interface SitePopupProps {
  props: SiteProperties;
  onClose?: () => void;
}

export function SitePopup({ props, onClose }: SitePopupProps) {
  return (
    <div className="site-popup" data-testid="site-popup">
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
          <span className="site-popup-value" data-testid="site-area">
            {props.area_km2.toFixed(2)} km&sup2;
          </span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Mean LSI</span>
          <span className="site-popup-value">{props.mean_lsi.toFixed(3)}</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Yield</span>
          <span className="site-popup-value" data-testid="site-yield">
            {props.kwh_per_kwp_yr.toFixed(0)} kWh/kWp/yr
          </span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Generation</span>
          <span className="site-popup-value">{props.gwh_per_yr.toFixed(2)} GWh/yr</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">LCOE</span>
          <span className="site-popup-value">{props.lcoe.toFixed(4)} USD/kWh</span>
        </div>
        <div className="site-popup-row">
          <span className="site-popup-label">Centroid</span>
          <span className="site-popup-value">
            {props.centroid_lat.toFixed(4)}, {props.centroid_lon.toFixed(4)}
          </span>
        </div>
      </div>
    </div>
  );
}
