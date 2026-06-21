"""Tests for the /version deploy-traceability endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solarsite.api import version as vermod
from solarsite.api.app import app


def test_env_sha_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLARSITE_GIT_SHA", "abc1234")
    monkeypatch.setenv("SOLARSITE_GIT_DESCRIBE", "deployed-20260621")
    info = vermod.get_version_info()
    assert info["git_sha"] == "abc1234"
    assert info["git_describe"] == "deployed-20260621"
    assert info["source"] == "env"


def test_version_file_used_when_no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SOLARSITE_GIT_SHA", raising=False)
    vf = tmp_path / "version.json"
    vf.write_text(json.dumps({"git_sha": "deadbee", "deployed_at": "2026-06-21T00:00:00Z"}))
    monkeypatch.setenv("SOLARSITE_VERSION_FILE", str(vf))
    info = vermod.get_version_info()
    assert info["git_sha"] == "deadbee"
    assert info["source"] == "file"


def test_endpoint_returns_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLARSITE_GIT_SHA", "feedface")
    client = TestClient(app)
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["git_sha"] == "feedface"
    assert "github.com" in body["repo"]


def test_never_raises_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SOLARSITE_GIT_SHA", raising=False)
    monkeypatch.setenv("SOLARSITE_VERSION_FILE", str(tmp_path / "missing.json"))
    # git may or may not resolve depending on the checkout; either a real sha or
    # the "unknown" fallback is acceptable — the call must not raise.
    info = vermod.get_version_info()
    assert "git_sha" in info and isinstance(info["git_sha"], str)
