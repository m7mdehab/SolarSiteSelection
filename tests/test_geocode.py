"""Tests for the server-side geocoding proxy (offline — upstream monkeypatched).

CI never touches the live Photon service; ``_photon_request`` is replaced. These
tests pin the normalisation, the bounded cache, and — most importantly — the
graceful-degradation contract: an upstream failure or a no-match must return a
``200`` with empty results and a note, never a 5xx.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from solarsite.api import geocode as geomod
from solarsite.api.app import app

# A minimal Photon-shaped response for "Cairo".
_CAIRO = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [31.2357, 30.0444]},
            "properties": {"name": "Cairo", "country": "Egypt", "state": "Cairo", "type": "city"},
        }
    ]
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    geomod._CACHE.clear()


def test_normalise_builds_label_and_coords() -> None:
    out = geomod._normalise(_CAIRO, 5)
    assert len(out) == 1
    assert out[0]["name"] == "Cairo"
    assert out[0]["lat"] == pytest.approx(30.0444)
    assert out[0]["lon"] == pytest.approx(31.2357)
    assert "Cairo" in out[0]["label"] and "Egypt" in out[0]["label"]


def test_geocode_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geomod, "_photon_request", lambda q, limit: _CAIRO)
    res = geomod.geocode("Cairo")
    assert res["results"][0]["name"] == "Cairo"
    assert "OpenStreetMap" in res["attribution"]
    assert "note" not in res  # results present → no fallback note


def test_geocode_short_query_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(q: str, limit: int) -> dict[str, Any]:
        raise AssertionError("must not call upstream for <2 chars")

    monkeypatch.setattr(geomod, "_photon_request", _boom)
    res = geomod.geocode("C")
    assert res["results"] == []
    assert "2 characters" in res["note"]


def test_geocode_empty_match_returns_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geomod, "_photon_request", lambda q, limit: {"features": []})
    res = geomod.geocode("zzzzzqqqq")
    assert res["results"] == []
    assert "Couldn't find" in res["note"]


def test_geocode_upstream_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(q: str, limit: int) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr(geomod, "_photon_request", _fail)
    res = geomod.geocode("Cairo")
    assert res["results"] == []
    assert "unavailable" in res["note"]


def test_geocode_caches_repeated_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _count(q: str, limit: int) -> dict[str, Any]:
        calls["n"] += 1
        return _CAIRO

    monkeypatch.setattr(geomod, "_photon_request", _count)
    geomod.geocode("Cairo")
    geomod.geocode("cairo")  # case-folded key → cache hit
    assert calls["n"] == 1


def test_api_geocode_endpoint_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geomod, "_photon_request", lambda q, limit: _CAIRO)
    client = TestClient(app)
    resp = client.get("/geocode", params={"q": "Cairo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["name"] == "Cairo"


def test_api_geocode_endpoint_failure_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(q: str, limit: int) -> dict[str, Any]:
        raise RuntimeError("down")

    monkeypatch.setattr(geomod, "_photon_request", _fail)
    client = TestClient(app)
    resp = client.get("/geocode", params={"q": "Cairo"})
    assert resp.status_code == 200  # graceful — never a 5xx
    assert resp.json()["results"] == []
