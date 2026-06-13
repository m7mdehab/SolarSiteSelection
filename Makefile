.PHONY: check lint type test test-cov live-smoke run dev deploy

check: lint type test

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

type:
	uv run pyright

test:
	uv run pytest -m "not live and not slow"

test-cov:
	uv run pytest -m "not live and not slow" --cov --cov-report=term-missing

live-smoke:
	uv run pytest -m live

run:
	uv run uvicorn solarsite.api.app:app --host 0.0.0.0 --port 7860

dev:
	uv run uvicorn solarsite.api.app:app --reload --port 7860

deploy:
	uv run python scripts/deploy_hf.py
