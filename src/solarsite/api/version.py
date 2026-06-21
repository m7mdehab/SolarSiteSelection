"""Deploy traceability — surface the exact GitHub commit a build came from.

The Hugging Face Space has its own git history, so the Space's commit SHA does
NOT correspond to any GitHub SHA; "what is running" was previously unprovable from
the repo. The deploy script bakes the GitHub ``HEAD`` SHA into ``data/version.json``
(uploaded to the Space) and/or the ``SOLARSITE_GIT_SHA`` env var; ``GET /version``
reads it so the live app self-reports its source commit.

Resolution order (first hit wins): ``SOLARSITE_GIT_SHA`` env → version file
(``$SOLARSITE_VERSION_FILE`` or ``<repo>/data/version.json``) → local ``git
rev-parse`` (dev checkout) → ``"unknown"``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["get_version_info"]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _from_env() -> dict[str, Any] | None:
    sha = os.environ.get("SOLARSITE_GIT_SHA")
    if not sha:
        return None
    return {
        "git_sha": sha.strip(),
        "git_describe": (os.environ.get("SOLARSITE_GIT_DESCRIBE") or "").strip() or None,
        "deployed_at": (os.environ.get("SOLARSITE_DEPLOYED_AT") or "").strip() or None,
        "source": "env",
    }


def _version_file() -> Path:
    override = os.environ.get("SOLARSITE_VERSION_FILE")
    if override:
        return Path(override)
    return _REPO_ROOT / "data" / "version.json"


def _from_file() -> dict[str, Any] | None:
    path = _version_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = str(data.get("git_sha") or "").strip()
    if not sha:
        return None
    return {
        "git_sha": sha,
        "git_describe": data.get("git_describe"),
        "deployed_at": data.get("deployed_at"),
        "source": "file",
    }


def _from_git() -> dict[str, Any] | None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not sha:
        return None
    try:
        describe = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        describe = None
    return {"git_sha": sha, "git_describe": describe, "deployed_at": None, "source": "git"}


def get_version_info() -> dict[str, Any]:
    """Best available source-commit identity for the running build."""
    info = _from_env() or _from_file() or _from_git()
    if info is None:
        info = {"git_sha": "unknown", "git_describe": None, "deployed_at": None, "source": "none"}
    info["repo"] = "https://github.com/m7mdehab/SolarSiteSelection"
    return info
