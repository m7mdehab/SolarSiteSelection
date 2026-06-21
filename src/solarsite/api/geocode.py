"""Server-side geocoding proxy for the consumer location search.

The browser must not call a public geocoder directly: that would leak the user's
keystrokes cross-origin, send no proper ``User-Agent`` (Nominatim's usage policy
requires one), and make CI depend on a live external service. Instead the SPA
calls ``GET /geocode?q=`` on our own backend, which forwards to Photon
(photon.komoot.io — OpenStreetMap data, built for autocomplete), normalises the
response, and caches recent queries.

Honesty / robustness contract
-----------------------------
* Results are real OpenStreetMap places (attribution returned alongside them).
* A network failure or an empty match is NOT an error to the caller: the endpoint
  returns ``200`` with ``results: []`` and a human-readable ``note`` so the UI can
  say "couldn't find that place — try again or click the map" and never breaks.
* The actual upstream call lives in :func:`_photon_request`, which tests
  monkeypatch — CI never touches the live service (offline rule).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import httpx

__all__ = ["OSM_ATTRIBUTION", "geocode"]

PHOTON_URL = "https://photon.komoot.io/api"
USER_AGENT = "SolarSiteSelection/1.0 (+https://github.com/m7mdehab/SolarSiteSelection)"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (geocoding via Photon / komoot)"

_TIMEOUT_S = 6.0
_CACHE_MAX = 256
# Recent (query, limit) -> normalised payload. Small bounded LRU.
_CACHE: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()


def _photon_request(query: str, limit: int) -> dict[str, Any]:
    """Raw Photon call. Isolated so tests can monkeypatch it (no live call in CI)."""
    resp = httpx.get(
        PHOTON_URL,
        params={"q": query, "limit": limit},
        headers={"User-Agent": USER_AGENT},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _label(props: dict[str, Any]) -> str:
    """Build a human label from Photon properties (name, locality, region, country)."""
    name = props.get("name") or ""
    parts = [
        props.get("city") or props.get("county") or props.get("state"),
        props.get("country"),
    ]
    tail = ", ".join(str(p) for p in parts if p and p != name)
    return f"{name}, {tail}" if tail else str(name)


def _normalise(raw: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for feat in raw.get("features", [])[:limit]:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        props = feat.get("properties") or {}
        name = props.get("name")
        if not name:
            continue
        out.append(
            {
                "label": _label(props),
                "name": str(name),
                "lat": lat,
                "lon": lon,
                "type": str(props.get("type") or props.get("osm_value") or "place"),
            }
        )
    return out


def geocode(query: str, limit: int = 5) -> dict[str, Any]:
    """Look up ``query`` and return ``{query, results, attribution, note?}``.

    Never raises for the caller: an empty query, an upstream outage, or a no-match
    all yield an empty ``results`` list with a ``note`` explaining what to do next.
    """
    q = (query or "").strip()
    limit = max(1, min(int(limit), 10))
    if len(q) < 2:
        return {
            "query": q,
            "results": [],
            "attribution": OSM_ATTRIBUTION,
            "note": "Type at least 2 characters to search.",
        }

    key = (q.lower(), limit)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached

    try:
        raw = _photon_request(q, limit)
        results = _normalise(raw, limit)
    except Exception:  # network/timeout/parse — degrade, do not surface a 5xx
        return {
            "query": q,
            "results": [],
            "attribution": OSM_ATTRIBUTION,
            "note": "Search is unavailable right now — try again or click the map.",
        }

    payload: dict[str, Any] = {"query": q, "results": results, "attribution": OSM_ATTRIBUTION}
    if not results:
        payload["note"] = "Couldn't find that place — try again or click the map."

    _CACHE[key] = payload
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return payload
