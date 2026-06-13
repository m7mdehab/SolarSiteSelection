"""Shared acquisition contract and helpers for every data source.

Every module in ``acquire/`` implements the same contract so the analysis layer
can treat sources uniformly:

    fetch(aoi: AOI, resolution_m: int = 100) -> xr.DataArray | gpd.GeoDataFrame

Returned data is ALWAYS:
  * in the working CRS chosen by :func:`solarsite.core.working_crs_for` (UTM zone
    of the AOI centroid) — rasters reprojected/aligned to the AOI GridSpec,
    vectors reprojected to the working CRS;
  * cached on disk via :class:`solarsite.core.DiskCache`, keyed by
    (source name, AOI hash, params).

Network sources subclass :class:`RasterSource` or :class:`VectorSource` and
implement ``_fetch_uncached``. They MUST use :func:`request_with_retry` for HTTP
so transient failures back off and retry, and they MUST be testable offline via
recorded fixtures (``respx`` for httpx, ``responses`` for requests) — CI never
hits live APIs.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

import geopandas as gpd
import httpx
import xarray as xr

from solarsite.core import AOI, DiskCache, GridSpec, working_crs_for

# Concrete layer type a source produces. Constrained (not bound) so it unifies
# with the disk cache's own constrained TypeVar.
V = TypeVar("V", xr.DataArray, gpd.GeoDataFrame)

__all__ = [
    "AcquisitionError",
    "DataSource",
    "RasterSource",
    "VectorSource",
    "grid_for_aoi",
    "request_with_retry",
]

# Default backoff schedule (seconds) for transient HTTP failures.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_DEFAULT_RETRIES = 4
_DEFAULT_BACKOFF = 1.5


class AcquisitionError(RuntimeError):
    """Raised when a data source cannot produce a result (after retries)."""


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = _DEFAULT_RETRIES,
    backoff: float = _DEFAULT_BACKOFF,
    sleep: Any = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request with exponential backoff on transient failures.

    Retries on connection errors and on the status codes in ``_RETRY_STATUS``.
    Raises :class:`AcquisitionError` once retries are exhausted. ``sleep`` is
    injectable so tests can run without real delays.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:  # connection/timeout level
            last_exc = exc
        else:
            if response.status_code not in _RETRY_STATUS:
                return response
            last_exc = AcquisitionError(f"{url} returned {response.status_code}")
        if attempt < retries:
            sleep(backoff * (2**attempt))
    raise AcquisitionError(f"Request to {url} failed after {retries + 1} attempts: {last_exc}")


def grid_for_aoi(aoi: AOI, resolution_m: int) -> GridSpec:
    """Build the working-CRS :class:`GridSpec` covering the AOI at a resolution.

    The AOI bounds (WGS-84) are projected to the working CRS and snapped to a
    grid of ``resolution_m`` cells. All raster sources align to this grid so
    layers stack cell-for-cell.
    """
    return GridSpec.from_aoi(aoi, resolution_m=resolution_m)


class DataSource(ABC, Generic[V]):
    """Base class for all acquisition sources.

    Generic over the concrete layer type ``V`` it produces (a DataArray for
    raster sources, a GeoDataFrame for vector sources). Subclasses set ``name``
    and implement ``_fetch_uncached``; ``fetch`` wraps it with disk caching keyed
    by (name, aoi.hash, params).
    """

    name: str

    def __init__(self, cache: DiskCache | None = None) -> None:
        self._cache = cache if cache is not None else DiskCache()

    def fetch(self, aoi: AOI, resolution_m: int = 100, **params: Any) -> V:
        """Return the layer for ``aoi`` at ``resolution_m``, cached on disk."""
        cache_params = {"resolution_m": resolution_m, **params}
        return self._cache.get_or_compute(
            source=self.name,
            aoi_hash=aoi.hash,
            params=cache_params,
            compute_fn=lambda: self._fetch_uncached(aoi, resolution_m, **params),
        )

    @abstractmethod
    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> V:
        """Produce the layer from the live source (no caching)."""
        raise NotImplementedError

    def working_crs(self, aoi: AOI) -> Any:
        """The working CRS for this AOI (UTM zone of the centroid)."""
        return working_crs_for(aoi.geometry)


class RasterSource(DataSource[xr.DataArray]):
    """A source whose ``fetch`` returns an :class:`xarray.DataArray` aligned to the AOI grid."""

    @abstractmethod
    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> xr.DataArray:
        raise NotImplementedError


class VectorSource(DataSource[gpd.GeoDataFrame]):
    """A source whose ``fetch`` returns a :class:`geopandas.GeoDataFrame` in the working CRS."""

    @abstractmethod
    def _fetch_uncached(self, aoi: AOI, resolution_m: int, **params: Any) -> gpd.GeoDataFrame:
        raise NotImplementedError
