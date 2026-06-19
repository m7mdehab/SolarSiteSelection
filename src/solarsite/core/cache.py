"""Disk cache for raster layers (xarray DataArrays) and vector layers (GeoDataFrames).

Cache keys are formed from (source, aoi_hash, params).  Each entry is stored
as a pair of files: a NetCDF file for DataArrays or a GeoPackage for
GeoDataFrames, plus a JSON sidecar recording the key metadata.

The cache root defaults to ``data/cache/`` relative to the project root,
which is gitignored.

Usage
-----
    from solarsite.core.cache import DiskCache

    cache = DiskCache()

    result = cache.get_or_compute(
        source="my_layer",
        aoi_hash="abc123",
        params={"resolution_m": 100},
        compute_fn=lambda: fetch_data(),
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import geopandas as gpd
import xarray as xr

__all__ = ["CacheKey", "DiskCache"]

log = logging.getLogger(__name__)

# Type alias for the supported cache value types
CacheValue = xr.DataArray | gpd.GeoDataFrame
T = TypeVar("T", xr.DataArray, gpd.GeoDataFrame)

# Default cache directory. Resolved at instantiation from $SOLARSITE_CACHE_DIR
# (absolute in the Docker image, e.g. /app/data/cache) so the cache location does
# NOT depend on the process CWD; falls back to "data/cache" relative to CWD.
_CACHE_DIR_ENV = "SOLARSITE_CACHE_DIR"
_DEFAULT_CACHE_ROOT_REL = "data/cache"


def _default_cache_root() -> Path:
    return Path(os.environ.get(_CACHE_DIR_ENV, _DEFAULT_CACHE_ROOT_REL))


class CacheKey:
    """Immutable cache key."""

    def __init__(self, source: str, aoi_hash: str, params: dict[str, object]) -> None:
        self.source = source
        self.aoi_hash = aoi_hash
        self.params = params

    @property
    def key_str(self) -> str:
        """Stable string representation of the key (used for filenames)."""
        params_json = json.dumps(self.params, sort_keys=True, separators=(",", ":"))
        raw = f"{self.source}|{self.aoi_hash}|{params_json}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def __repr__(self) -> str:
        return (
            f"CacheKey(source={self.source!r}, aoi_hash={self.aoi_hash!r}, params={self.params!r})"
        )


class DiskCache:
    """File-system cache for DataArrays and GeoDataFrames.

    Args:
        root: Cache root directory.  Created on first write if it doesn't exist.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _default_cache_root()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_compute(
        self,
        source: str,
        aoi_hash: str,
        params: dict[str, object],
        compute_fn: Callable[[], T],
    ) -> T:
        """Return cached value or invoke compute_fn, store result, return it.

        Args:
            source: Logical data-source identifier (e.g. ``"dem"``, ``"wdpa"``).
            aoi_hash: Stable hash of the AOI geometry.
            params: Additional parameters that affect the output (e.g. resolution).
            compute_fn: Zero-arg callable that produces the value on cache miss.

        Returns:
            The cached or freshly computed value.
        """
        key = CacheKey(source, aoi_hash, params)
        cached = self._load(key)
        if cached is not None:
            log.debug("Cache HIT  %s", key)
            return cached  # type: ignore[return-value]

        log.debug("Cache MISS %s", key)
        value = compute_fn()
        self._store(key, value)
        return value

    def exists(self, source: str, aoi_hash: str, params: dict[str, object]) -> bool:
        """Check whether a cache entry exists without loading it."""
        key = CacheKey(source, aoi_hash, params)
        return self._meta_path(key).exists()

    def invalidate(self, source: str, aoi_hash: str, params: dict[str, object]) -> None:
        """Delete a cache entry if it exists."""
        key = CacheKey(source, aoi_hash, params)
        meta = self._meta_path(key)
        if meta.exists():
            raw_path = self._read_meta(key).get("data_path")
            if raw_path is not None:
                data_path = Path(str(raw_path))
                if data_path.exists():
                    data_path.unlink()
            meta.unlink()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _meta_path(self, key: CacheKey) -> Path:
        return self.root / f"{key.key_str}.json"

    def _data_stem(self, key: CacheKey) -> Path:
        return self.root / key.key_str

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _store(self, key: CacheKey, value: CacheValue) -> None:
        self._ensure_root()
        meta: dict[str, object] = {
            "source": key.source,
            "aoi_hash": key.aoi_hash,
            "params": key.params,
        }

        if isinstance(value, xr.DataArray):
            data_path = self._data_stem(key).with_suffix(".nc")
            # A DataArray carrying a `spatial_ref` coord (from rioxarray.write_crs)
            # serialises that coord as a second NetCDF variable, which breaks
            # `xr.open_dataarray` on reload. Persist the array's name so `_load`
            # can select it back out of an open_dataset() and treat spatial_ref
            # as a coordinate.
            to_save = value if value.name is not None else value.rename("data")
            to_save.to_netcdf(str(data_path))
            meta["dtype"] = "DataArray"
            meta["data_path"] = str(data_path)
            meta["da_name"] = str(to_save.name)
        elif isinstance(value, gpd.GeoDataFrame):
            data_path = self._data_stem(key).with_suffix(".gpkg")
            value.to_file(str(data_path), driver="GPKG")
            meta["dtype"] = "GeoDataFrame"
            meta["data_path"] = str(data_path)
        else:
            raise TypeError(f"Unsupported value type: {type(value)}")

        meta_path = self._meta_path(key)
        meta_path.write_text(json.dumps(meta, indent=2))

    def _load(self, key: CacheKey) -> CacheValue | None:
        meta_path = self._meta_path(key)
        if not meta_path.exists():
            return None

        meta = self._read_meta(key)
        data_path = Path(str(meta["data_path"]))
        if not data_path.exists():
            return None

        dtype = str(meta.get("dtype", ""))
        if dtype == "DataArray":
            # Open as a Dataset with decode_coords="all" so a `spatial_ref`
            # grid-mapping variable is restored as a coordinate (not a data var),
            # then select the original array by its persisted name.
            ds = xr.open_dataset(str(data_path), decode_coords="all")
            da_name = meta.get("da_name")
            if da_name is not None and da_name in ds.data_vars:
                da = ds[str(da_name)]
            else:
                da = ds[next(iter(ds.data_vars))]
            # Load into memory so the file handle is released
            return da.load()
        elif dtype == "GeoDataFrame":
            return gpd.read_file(str(data_path))
        else:
            raise ValueError(f"Unknown cached dtype '{dtype}' in {meta_path}")

    def _read_meta(self, key: CacheKey) -> dict[str, object]:
        return json.loads(self._meta_path(key).read_text())  # type: ignore[return-value]
