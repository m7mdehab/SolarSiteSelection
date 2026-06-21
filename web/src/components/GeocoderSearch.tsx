import { useEffect, useRef, useState } from 'react';
import { geocode } from '../api/client';
import type { GeocodeResult } from '../types/api';

interface GeocoderSearchProps {
  onSelect: (lat: number, lon: number, label: string) => void;
}

/** Debounced place-search box. Calls the backend /geocode proxy (OSM via Photon)
 *  and shows suggestions. A no-match or an upstream outage shows the server's
 *  human-readable note ("couldn't find that place — try again or click the map")
 *  rather than breaking — the map-pin fallback always remains available. */
export function GeocoderSearch({ onSelect }: GeocoderSearchProps) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState<GeocodeResult[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // Guards against an out-of-order slow response overwriting a newer query.
  const seq = useRef(0);

  useEffect(() => {
    const query = q.trim();
    const myseq = ++seq.current;
    // All state updates happen in the deferred callback (never synchronously in
    // the effect body), so a fast typist never triggers cascading renders.
    const timer = setTimeout(
      () => {
        if (query.length < 2) {
          setResults([]);
          setNote(null);
          setBusy(false);
          return;
        }
        setBusy(true);
        geocode(query)
          .then((res) => {
            if (myseq !== seq.current) return; // a newer keystroke superseded us
            setResults(res.results);
            setNote(res.results.length === 0 ? (res.note ?? 'No matches.') : null);
            setOpen(true);
          })
          .catch(() => {
            if (myseq !== seq.current) return;
            setResults([]);
            setNote('Search is unavailable right now — try again or click the map.');
            setOpen(true);
          })
          .finally(() => {
            if (myseq === seq.current) setBusy(false);
          });
      },
      query.length < 2 ? 0 : 300
    );
    return () => clearTimeout(timer);
  }, [q]);

  function pick(r: GeocodeResult) {
    onSelect(r.lat, r.lon, r.label);
    setQ(r.label);
    setOpen(false);
    setResults([]);
    setNote(null);
  }

  return (
    <div className="geocoder">
      <input
        className="geocoder-input"
        data-testid="geocoder-input"
        type="text"
        value={q}
        placeholder="Search a place — e.g. Cairo, or your address"
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length > 0 && setOpen(true)}
        autoComplete="off"
      />
      {busy && <span className="geocoder-busy" data-testid="geocoder-busy">…</span>}
      {open && (results.length > 0 || note) && (
        <div className="geocoder-results" data-testid="geocoder-results">
          {results.map((r, i) => (
            <button
              key={`${r.label}-${i}`}
              type="button"
              className="geocoder-suggestion"
              data-testid="geocoder-suggestion"
              onClick={() => pick(r)}
            >
              {r.label}
            </button>
          ))}
          {results.length === 0 && note && (
            <div className="geocoder-note" data-testid="geocoder-note">
              {note}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
