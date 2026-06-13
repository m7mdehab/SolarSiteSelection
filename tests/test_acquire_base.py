"""Tests for the shared acquisition contract (src/solarsite/acquire/base.py)
and the GridSpec.from_aoi integration helper."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from solarsite.acquire.base import AcquisitionError, grid_for_aoi, request_with_retry
from solarsite.core import AOI, GridSpec

_FIXTURE = Path(__file__).parent / "fixtures" / "nw_coast_aoi.geojson"


def _aoi() -> AOI:
    return AOI.from_geojson(json.loads(_FIXTURE.read_text()))


def test_nw_coast_fixture_is_valid_and_within_limit() -> None:
    aoi = _aoi()
    assert 0 < aoi.area_km2 < 10_000  # under the AOI cap
    assert aoi.area_km2 == pytest.approx(5280.6, rel=0.02)


def test_grid_for_aoi_is_utm_and_covers_aoi() -> None:
    aoi = _aoi()
    grid = grid_for_aoi(aoi, resolution_m=100)
    assert isinstance(grid, GridSpec)
    # NW-coast centroid (~27.5E) falls in UTM zone 35N.
    assert grid.crs.to_epsg() == 32635
    assert grid.resolution_m == 100
    # Grid bounds snap to whole cells and have positive extent.
    assert grid.width > 0 and grid.height > 0
    assert (grid.minx % 100 == 0) and (grid.maxy % 100 == 0)


def test_grid_resolution_scales_cell_count() -> None:
    aoi = _aoi()
    g100 = grid_for_aoi(aoi, 100)
    g200 = grid_for_aoi(aoi, 200)
    # Halving resolution roughly quarters the cell count.
    assert g200.width == pytest.approx(g100.width / 2, abs=2)


def test_request_with_retry_succeeds_first_try() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resp = request_with_retry(client, "GET", "https://example.test/data", sleep=lambda _: None)
    assert resp.status_code == 200
    assert calls["n"] == 1


def test_request_with_retry_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200, text="x")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resp = request_with_retry(client, "GET", "https://example.test/d", sleep=lambda _: None)
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_request_with_retry_exhausts_and_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="err")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AcquisitionError):
        request_with_retry(client, "GET", "https://example.test/d", retries=2, sleep=lambda _: None)
