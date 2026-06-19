# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — build the React/Vite frontend
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build   # → /web/dist

# ---------------------------------------------------------------------------
# Stage 2 — Python runtime (FastAPI + analysis engine + WeasyPrint)
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# WeasyPrint 60+ renders via Pango + FreeType (libpangoft2); fonts required.
# rasterio/pyogrio/shapely ship manylinux wheels with bundled GDAL/GEOS/PROJ,
# so no apt GDAL build is needed (per the plan: wheels not apt-compiled).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
        fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, locked installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    SOLARSITE_WEB_DIST=/app/web/dist

WORKDIR /app

# Install dependencies first (cache layer) from the lockfile, runtime only.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Application code + config + the prebuilt frontend.
COPY src/ ./src/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY --from=web /web/dist ./web/dist

# Install the project itself into the venv.
RUN uv sync --locked --no-dev

# Bake the preset-AOI offline cache so the public demo needs zero third-party API
# calls at runtime. In CI this is just the .gitkeep marker (the real cache is
# gitignored); at deploy (P4.4) the HF Space build context carries the real
# ~2 MB cache, uploaded by scripts/deploy_hf.py.
COPY data/cache/ ./data/cache/

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:7860/health', timeout=3).status==200 else 1)"

CMD ["uv", "run", "--no-dev", "uvicorn", "solarsite.api.app:app", "--host", "0.0.0.0", "--port", "7860"]
