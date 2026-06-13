"""Prepare the WDPA protected-areas layer for the pipeline.

The WDPA dataset requires a manual license click-through, so it cannot be
auto-downloaded. Download the country shapefile bundle from
https://www.protectedplanet.net (e.g. ``WDPA_WDOECM_<month>_Public_<ISO3>_shp.zip``),
then run::

    uv run python scripts/prepare_wdpa.py path/to/WDPA_..._shp.zip

The bundle contains three split shapefile shards (WDPA distributes large
countries that way); this script merges every polygon shard into a single
``data/manual/wdpa_egypt.gpkg`` that the acquisition layer reads.
"""

from __future__ import annotations

import argparse
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

DEFAULT_OUT = Path("data/manual/wdpa_egypt.gpkg")


def prepare(bundle: Path, out: Path = DEFAULT_OUT) -> Path:
    """Merge all polygon shapefiles in a WDPA bundle zip into one GeoPackage."""
    frames: list[gpd.GeoDataFrame] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(bundle) as zf:
            zf.extractall(tmp_path)
        # WDPA ships shards as nested zips: WDPA_..._shp_0.zip, _1.zip, _2.zip
        for shard in sorted(tmp_path.glob("*_shp_*.zip")):
            shard_dir = tmp_path / shard.stem
            with zipfile.ZipFile(shard) as zf:
                zf.extractall(shard_dir)
            for shp in sorted(shard_dir.glob("*polygons.shp")):
                frames.append(gpd.read_file(shp))
        if not frames:  # flat bundle (small countries): polygons shp at top level
            for shp in sorted(tmp_path.rglob("*polygons.shp")):
                frames.append(gpd.read_file(shp))
    if not frames:
        msg = f"No polygon shapefiles found in {bundle}"
        raise FileNotFoundError(msg)
    merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.to_file(out, driver="GPKG")
    print(f"Wrote {len(merged)} protected-area polygons to {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="WDPA *_shp.zip bundle from protectedplanet.net")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    prepare(args.bundle, args.out)


if __name__ == "__main__":
    main()
