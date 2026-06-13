import type { JobResponse, StageStatus } from '../types/api';
import '../styles/ProgressView.css';

interface ProgressViewProps {
  job: JobResponse;
}

function StageIcon({ status }: { status: StageStatus }) {
  const icons: Record<StageStatus, string> = {
    pending: 'o',
    running: '~',
    done: 'v',
    failed: 'x',
  };
  return <span className={`stage-icon stage-icon-${status}`}>{icons[status]}</span>;
}

export function ProgressView({ job }: ProgressViewProps) {
  return (
    <div className="progress-view" data-testid="progress-view">
      <div className="progress-overall">
        <span className="progress-status-label">Status:</span>
        <span className={`progress-status-badge progress-status-${job.status}`}>{job.status}</span>
        {job.error && <div className="progress-error">{job.error}</div>}
      </div>

      {job.acquire_stages.length > 0 && (
        <div className="progress-section">
          <div className="progress-section-title">Data Acquisition</div>
          {job.acquire_stages.map((stage) => (
            <div key={stage.source} className="progress-stage">
              <StageIcon status={stage.status} />
              <span className="progress-stage-source">{stage.source}</span>
              {stage.error && <span className="progress-stage-error">{stage.error}</span>}
            </div>
          ))}
        </div>
      )}

      {job.analysis_status && (
        <div className="progress-section">
          <div className="progress-section-title">Analysis</div>
          <div className="progress-stage">
            <StageIcon status={job.analysis_status} />
            <span className="progress-stage-source">Suitability Analysis</span>
            {job.analysis_error && (
              <span className="progress-stage-error">{job.analysis_error}</span>
            )}
          </div>
        </div>
      )}

      {job.status === 'done' && job.n_sites !== undefined && (
        <div className="progress-done-msg">
          Analysis complete. Found <strong>{job.n_sites}</strong> candidate site
          {job.n_sites !== 1 ? 's' : ''}.
        </div>
      )}

      {job.skipped_sources && job.skipped_sources.length > 0 && (
        <div className="progress-skipped">
          Skipped sources: {job.skipped_sources.join(', ')}
        </div>
      )}
    </div>
  );
}
