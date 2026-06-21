import { useEffect, useState } from 'react';
import { getVersion } from '../api/client';
import type { VersionInfo } from '../types/api';

/** Footer surfacing the source GitHub commit of the running build, so "what is
 *  deployed" is verifiable from the UI (the HF Space has its own git history that
 *  does not correspond to any GitHub SHA). Fails silently if /version is absent. */
export function VersionFooter() {
  const [info, setInfo] = useState<VersionInfo | null>(null);

  useEffect(() => {
    getVersion()
      .then(setInfo)
      .catch(() => setInfo(null));
  }, []);

  if (!info || info.git_sha === 'unknown') return null;
  const short = info.git_sha.slice(0, 7);
  const commitUrl = `${info.repo}/commit/${info.git_sha}`;
  return (
    <footer className="app-version-footer" data-testid="version-footer">
      <span>
        source{' '}
        <a href={commitUrl} target="_blank" rel="noreferrer" data-testid="version-sha">
          {info.git_describe || short}
        </a>
      </span>
      {info.deployed_at && <span className="app-version-date"> · deployed {info.deployed_at}</span>}
    </footer>
  );
}
