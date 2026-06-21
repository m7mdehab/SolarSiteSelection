"""Deploy SolarSiteSelection to a Hugging Face Space (Docker SDK).

The Space is built from this repo's Dockerfile and serves the API + SPA on port
7860. The preset-AOI offline cache (``data/cache/``, gitignored) is uploaded so
the public demo runs without any third-party API calls.

Usage::

    # HF_TOKEN must be set (read from .env automatically)
    uv run python scripts/deploy_hf.py --space m7mdehab/solar-site-selection

    # Dry run — print what would be uploaded, do not touch HF:
    uv run python scripts/deploy_hf.py --space <id> --dry-run

The Space README needs Hugging Face Docker-SDK front-matter; this script
generates it (the GitHub README has no such front-matter). Everything required
to build the image is uploaded; local-only dirs (``_pm/``, ``.git/``,
``node_modules``, ``.venv``) are excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    """Run a git command in the repo, returning stripped stdout ('' on failure)."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _write_version_file() -> dict[str, str]:
    """Bake the source GitHub commit into data/version.json (for GET /version).

    The HF Space has its own git history, so the Space SHA is not a GitHub SHA;
    this file lets the running build self-report the exact GitHub commit it was
    built from. Uploaded to the Space and copied into the image by the Dockerfile.
    """
    sha = _git("rev-parse", "HEAD") or "unknown"
    describe = _git("describe", "--tags", "--always", "--dirty")
    info = {
        "git_sha": sha,
        "git_describe": describe,
        "deployed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = REPO_ROOT / "data" / "version.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


# HF Space README front-matter (Docker SDK, port 7860).
_SPACE_HEADER = """---
title: SolarSiteSelection
emoji: ☀️
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# SolarSiteSelection — live demo

Draw an area on a map, get a defensible PV siting analysis. This Space serves the
FastAPI back-end and the React/MapLibre front-end on port 7860. The bundled
offline cache lets the preset Northwest-Coast-of-Egypt AOI run end-to-end without
any third-party API calls.

Source & full documentation: https://github.com/m7mdehab/SolarSiteSelection
"""

# Paths to upload (relative to repo root). The Dockerfile builds the frontend
# from web/ source, so node_modules/dist are NOT needed.
_INCLUDE = [
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "src",
    "configs",
    "scripts",
    "web/src",
    "web/public",
    "web/index.html",
    "web/package.json",
    "web/package-lock.json",
    "web/tsconfig.json",
    "web/tsconfig.app.json",
    "web/tsconfig.node.json",
    "web/vite.config.ts",
    "web/eslint.config.js",
    "data/cache",  # the real ~2 MB offline demo cache (gitignored locally)
    "data/version.json",  # deploy-traceability file (written at deploy time)
]


def _load_env() -> None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _gather_files(dry_run: bool) -> list[Path]:
    files: list[Path] = []
    for rel in _INCLUDE:
        p = REPO_ROOT / rel
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
        elif not dry_run:
            print(f"  warning: {rel} not found, skipping", file=sys.stderr)
    return files


def deploy(space_id: str, *, dry_run: bool, private: bool) -> int:
    _load_env()
    token = os.environ.get("HF_TOKEN")
    if not token and not dry_run:
        print("ERROR: HF_TOKEN not set (put it in .env or the environment).", file=sys.stderr)
        return 2

    version = _write_version_file()
    print(
        f"Source commit: {version['git_sha']} ({version['git_describe']}) "
        f"@ {version['deployed_at']}"
    )

    cache_dir = REPO_ROOT / "data" / "cache"
    cache_files = [f for f in cache_dir.rglob("*") if f.is_file() and f.name != ".gitkeep"]
    if not cache_files:
        print(
            "WARNING: data/cache has no seeded layers — the public demo will not be offline.\n"
            "Seed first:  uv run python scripts/demo_aoi.py "
            "--aoi tests/fixtures/nw_coast_aoi.geojson --resolution 500",
            file=sys.stderr,
        )

    files = _gather_files(dry_run)
    print(f"Space: {space_id}")
    print(f"Files to upload: {len(files)} (incl. {len(cache_files)} cached demo layers)")

    if dry_run:
        for f in files[:40]:
            print(f"  {f.relative_to(REPO_ROOT)}")
        if len(files) > 40:
            print(f"  ... and {len(files) - 40} more")
        print("Dry run — nothing uploaded.")
        return 0

    from huggingface_hub import HfApi  # imported here so --dry-run needs no dep

    api = HfApi(token=token)
    print("Creating/ensuring Space exists ...")
    api.create_repo(
        repo_id=space_id,
        repo_type="space",
        space_sdk="docker",
        private=private,
        exist_ok=True,
    )

    # Upload the generated Space README (front-matter) first.
    api.upload_file(
        path_or_fileobj=_SPACE_HEADER.encode("utf-8"),
        path_in_repo="README.md",
        repo_id=space_id,
        repo_type="space",
    )

    # Upload everything else, preserving repo-relative paths.
    print("Uploading files ...")
    for f in files:
        rel = f.relative_to(REPO_ROOT).as_posix()
        api.upload_file(
            path_or_fileobj=str(f),
            path_in_repo=rel,
            repo_id=space_id,
            repo_type="space",
        )
    print(f"Done. Space will build at: https://huggingface.co/spaces/{space_id}")
    print("Track build status with: huggingface_hub.HfApi().space_info(...).runtime.stage")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--space",
        default="M7mdehab/solar-site-selection",
        help="HF Space id (owner/name)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List files, do not upload")
    parser.add_argument("--private", action="store_true", help="Create the Space as private")
    args = parser.parse_args()
    return deploy(args.space, dry_run=args.dry_run, private=args.private)


if __name__ == "__main__":
    sys.exit(main())
