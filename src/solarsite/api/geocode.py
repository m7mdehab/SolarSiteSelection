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

__all__ = ["OSM_ATTRIBUTION", "geocode", "geocode_reverse"]

PHOTON_URL = "https://photon.komoot.io/api"
PHOTON_REVERSE_URL = "https://photon.komoot.io/reverse"
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
        # Over-fetch so the prominence re-rank has candidates to reorder, then trim.
        params={"q": query, "limit": max(limit, 10)},
        headers={"User-Agent": USER_AGENT},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _photon_reverse_request(lat: float, lon: float) -> dict[str, Any]:
    """Raw Photon reverse call. Isolated so tests can monkeypatch it."""
    resp = httpx.get(
        PHOTON_REVERSE_URL,
        params={"lat": lat, "lon": lon},
        headers={"User-Agent": USER_AGENT},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


# Prominence ranking. Photon returns no population/importance field, so "Cairo"
# can surface a tiny US Cairo before Cairo, Egypt. We re-rank by place type and
# geographic extent (a larger bbox ≈ a more prominent place) so the obvious city
# comes first; every result is still labelled with region + country regardless.
_PLACE_RANK: dict[str, int] = {
    "city": 6,
    "municipality": 5,
    "town": 4,
    "borough": 4,
    "village": 3,
    "suburb": 2,
    "hamlet": 2,
    "locality": 1,
}


def _prominence(props: dict[str, Any]) -> float:
    """Heuristic prominence score for a Photon feature (higher = more prominent)."""
    osm_key = props.get("osm_key")
    osm_value = str(props.get("osm_value") or props.get("type") or "")
    score = 0.0
    if osm_key == "place":
        score += _PLACE_RANK.get(osm_value, 0) * 10.0
    elif osm_key in ("boundary",):  # administrative areas (states/countries)
        score += 8.0
    elif osm_key in ("highway", "building", "address"):
        score -= 5.0  # streets/houses are rarely the intended "where I live" pick
    extent = props.get("extent")  # [west, north, east, south]
    if isinstance(extent, list) and len(extent) == 4:
        try:
            w, n, e, s = (float(x) for x in extent)
            score += min(abs(e - w) * abs(n - s), 25.0)  # bbox area, capped
        except (TypeError, ValueError):
            pass
    return score


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
    scored: list[tuple[float, dict[str, Any]]] = []
    for feat in raw.get("features", []):
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
        scored.append(
            (
                _prominence(props),
                {
                    "label": _label(props),
                    "name": str(name),
                    "lat": lat,
                    "lon": lon,
                    "type": str(props.get("type") or props.get("osm_value") or "place"),
                },
            )
        )
    # Stable sort by prominence (descending) — Photon's relevance order is the
    # tiebreak, so equally-prominent matches keep their original ranking.
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:limit]]


def geocode_reverse(lat: float, lon: float) -> dict[str, Any]:
    """Reverse-geocode a pin to a place name. Returns ``{label, lat, lon}`` or a
    coordinate label on miss/outage (never raises — the pin always gets a label)."""
    fallback = f"Pinned: {lat:.4f}, {lon:.4f}"
    try:
        raw = _photon_reverse_request(lat, lon)
        feats = raw.get("features") or []
        if feats:
            props = feats[0].get("properties") or {}
            label = _label(props)
            if label:
                return {"label": label, "lat": lat, "lon": lon, "attribution": OSM_ATTRIBUTION}
    except Exception:  # network/timeout/parse — fall back to coordinates
        pass
    return {"label": fallback, "lat": lat, "lon": lon, "attribution": OSM_ATTRIBUTION}


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
