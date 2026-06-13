"""Acquire every input layer for an AOI and assemble an aligned xarray Dataset.

This is the Phase-1 integration harness: it drives all five acquisition sources
through the shared contract and stacks their outputs — all on the same
working-CRS grid (``grid_for_aoi``) — into a single multi-layer ``xr.Dataset``.

Usage
-----
Seed the on-disk cache from the live APIs (one real call per source)::

    uv run python scripts/demo_aoi.py --aoi tests/fixtures/nw_coast_aoi.geojson --resolution 500

Then reproduce the Dataset entirely from cache, no network (add ``--offline``)::

    uv run python scripts/demo_aoi.py --aoi tests/fixtures/nw_coast_aoi.geojson --offline

Acquisition is resilient: a layer whose source is down (or, in ``--offline``
mode, not yet cached) is skipped with a warning and reported, rather than
aborting the whole run. The run never silently falls back to the network offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import xarray as xr

from solarsite.acquire.base import grid_for_aoi
from solarsite.acquire.climate import ClimateSource
from solarsite.acquire.landcover import WDPASource, WorldCoverSource, exclusion_mask
from solarsite.acquire.osm import (
    OSMPowerSource,
    OSMRailwaySource,
    OSMRoadsSource,
    OSMUrbanSource,
    proximity_for,
)
from solarsite.acquire.pvgis import PVGISSource
from solarsite.acquire.terrain import TerrainSource
from solarsite.core import AOI, DiskCache


def _load_aoi(path: Path) -> AOI:
    return AOI.from_geojson(json.loads(path.read_text()))


def build_dataset(
    aoi: AOI, resolution_m: int, cache: DiskCache, *, offline: bool = False
) -> tuple[xr.Dataset, list[str]]:
    """Acquire every available layer and stack them into one aligned Dataset.

    Resilient by design: a source that fails (e.g. a third-party API outage) or
    that is absent from the cache in ``offline`` mode is skipped with a warning
    rather than aborting the whole pipeline. Returns the Dataset plus the list of
    skipped layer names so the caller can report/track them.
    """
    grid = grid_for_aoi(aoi, resolution_m)
    layers: dict[str, xr.DataArray] = {}
    skipped: list[str] = []

    def acquire(label: str, cache_names: list[str], thunk: Any) -> None:
        """Run one layer acquisition, guarding for offline-misses and failures."""
        if offline and not all(
            cache.exists(n, aoi.hash, {"resolution_m": resolution_m}) for n in cache_names
        ):
            print(f"  [skip] {label}: not in cache (offline)", file=sys.stderr)
            skipped.append(label)
            return
        try:
            for name, da in thunk().items():
                layers[name] = da
        except Exception as exc:  # degrade gracefully on any source error
            print(f"  [skip] {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped.append(label)

    def _split_bands(da: xr.DataArray, bands: tuple[str, ...]) -> dict[str, xr.DataArray]:
        return {b: da.sel(band=b).drop_vars("band") for b in bands}

    acquire(
        "solar",
        ["pvgis"],
        lambda: {"ghi_annual": PVGISSource(cache=cache).fetch(aoi, resolution_m)},
    )
    acquire(
        "terrain",
        ["terrain"],
        lambda: _split_bands(
            TerrainSource(cache=cache).fetch(aoi, resolution_m),
            ("elevation", "slope", "aspect_class"),
        ),
    )
    acquire(
        "climate",
        ["openmeteo"],
        lambda: _split_bands(
            ClimateSource(cache=cache).fetch(aoi, resolution_m),
            ("temperature", "humidity", "wind_speed"),
        ),
    )

    wc_source = WorldCoverSource(cache=cache)
    acquire("lulc", ["worldcover"], lambda: {"lulc": wc_source.fetch(aoi, resolution_m)})
    acquire(
        "exclusion_mask",
        ["worldcover", "wdpa"],
        lambda: {
            "exclusion_mask": exclusion_mask(
                aoi, resolution_m, worldcover_source=wc_source, wdpa_source=WDPASource(cache=cache)
            )
        },
    )

    osm_sources = {
        "dist_power": OSMPowerSource(cache=cache),
        "dist_roads": OSMRoadsSource(cache=cache),
        "dist_railway": OSMRailwaySource(cache=cache),
        "dist_urban": OSMUrbanSource(cache=cache),
    }
    for var, src in osm_sources.items():
        acquire(var, [src.name], lambda v=var, s=src: {v: proximity_for(s, aoi, resolution_m)})

    if not layers:
        raise SystemExit("No layers could be acquired (all sources failed or uncached).")

    # All layers share `grid`; align defensively on the shared coords, then assemble.
    ref = next(iter(layers.values()))
    aligned = {
        name: da.assign_coords(x=ref.x, y=ref.y) if da.shape == ref.shape else da
        for name, da in layers.items()
    }
    ds = xr.Dataset(aligned)
    ds.attrs["crs"] = f"EPSG:{grid.crs.to_epsg()}"
    ds.attrs["resolution_m"] = resolution_m
    ds.attrs["aoi_hash"] = aoi.hash
    ds.attrs["aoi_area_km2"] = round(aoi.area_km2, 1)
    return ds, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aoi", type=Path, required=True, help="Path to an AOI GeoJSON file")
    parser.add_argument("--resolution", type=int, default=500, help="Grid resolution in metres")
    parser.add_argument(
        "--offline", action="store_true", help="Read only from the on-disk cache (no network)"
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("data/cache"), help="DiskCache root directory"
    )
    parser.add_argument("--out", type=Path, default=None, help="Optional path to save the Dataset")
    args = parser.parse_args(argv)

    aoi = _load_aoi(args.aoi)
    cache = DiskCache(args.cache_dir)

    ds, skipped = build_dataset(aoi, args.resolution, cache, offline=args.offline)

    print(f"AOI: {args.aoi.name}  area={aoi.area_km2:.1f} km²  {ds.attrs['crs']}")
    print(f"Resolution: {args.resolution} m   Grid: {ds.sizes}")
    print(f"Layers ({len(ds.data_vars)}):")
    for name, da in ds.data_vars.items():
        print(f"  - {name:16s} shape={tuple(da.shape)} dtype={da.dtype}")
    if skipped:
        print(f"Skipped ({len(skipped)}): {', '.join(skipped)}")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        ds.to_netcdf(args.out)
        print(f"Saved Dataset → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
