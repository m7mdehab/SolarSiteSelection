import { useAppContext } from '../context/AppContext';
import { getSitesGeoJsonUrl, getLayerPngUrl, getReportPdfUrl } from '../api/client';
import '../styles/DownloadButtons.css';

export function DownloadButtons() {
  const { state } = useAppContext();
  const jobId = state.jobId;

  if (!jobId || state.job?.status !== 'done') return null;

  return (
    <div className="download-buttons">
      <div className="download-title">Export</div>
      <a
        href={getSitesGeoJsonUrl(jobId)}
        download="sites.geojson"
        className="download-btn"
        data-testid="download-geojson"
      >
        Download GeoJSON
      </a>
      <a
        href={getLayerPngUrl(jobId, 'lsi')}
        download="lsi.png"
        className="download-btn"
        data-testid="download-png"
        title="Note: PNG layer (GeoTIFF export via report)"
      >
        Download LSI PNG
      </a>
      <a
        href={getReportPdfUrl(jobId)}
        download="report.pdf"
        className="download-btn download-btn-primary"
        data-testid="download-pdf"
      >
        Download PDF Report
      </a>
    </div>
  );
}
