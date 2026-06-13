"""PVGIS solar resource acquisition module (P1.1).

Queries the European Commission PVGIS REST API (no key required) to fetch
monthly and annual GHI (Global Horizontal Irradiation) for an AOI.

Because PVGIS is point-based, this module:

1. Creates a coarse **sampling lattice** over the AOI in geographic coordinates.
2. Queries the PVGIS ``MRcalc`` endpoint once per sample point.
3. **Interpolates** the point-based annual GHI values onto the full working-CRS
   grid using ``scipy.interpolate.griddata`` with ``method='linear'``, falling
   back to ``method='nearest'`` for any exterior pixels that the convex hull of
   the samples does not cover (NaN fill after linear step).

Sampling density
----------------
Default ``sample_n=5`` -> 5x5 = 25 sample points.  For a 10,000 km2 AOI that
is roughly one point per 400 km2, or ~20 km spacing -- sufficient for the
smooth GHI field which varies over hundreds of kilometres in Egypt.  Users may
set ``sample_n`` to a higher value for larger or more complex AOIs.

PVGIS API endpoints used
------------------------
* **MRcalc**: ``https://re.jrc.ec.europa.eu/api/v5_2/MRcalc``
  - Parameters: ``lat``, ``lon``, ``startyear=2005``, ``endyear=2020``,
    ``horirrad=1`` (horizontal irradiation), ``outputformat=json``.
  - Response field ``H(h)_m`` in the monthly array: kWh/m2/month.
  - Annual GHI is the mean of per-year sums (kWh/m2/year).

* **TMY**: ``https://re.jrc.ec.europa.eu/api/v5_2/tmy``
  - Parameters: ``lat``, ``lon``, ``outputformat=json``.
  - Response ``outputs.tmy_hourly``: list of hourly dicts with keys
    ``time(UTC)``, ``G(h)`` (W/m2), ``T2m``, ``WS10m``, etc.

GHI unit
--------
The returned DataArray stores **annual GHI in kWh/m2/year**.
Typical NW-Egypt range: 1900-2300 kWh/m2/year (~5-6.3 kWh/m2/day).
"""

from __future__ import annotations

import logging
import time as _time_module
from collections import defaultdict
from typing import Any

import httpx
import numpy as np
import pandas as pd
import rioxarray  # noqa: F401 — registers the .rio accessor on xarray objects
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import griddata

from solarsite.acquire.base import AcquisitionError, RasterSource, grid_for_aoi, request_with_retry
from solarsite.core import AOI, DiskCache, empty_dataarray

__all__ = ["PVGISSource"]

log = logging.getLogger(__name__)

# PVGIS v5.2 base URL
_BASE_URL = "https://re.jrc.ec.europa.eu/api/v5_2"
_MRCALC_URL = f"{_BASE_URL}/MRcalc"
_TMY_URL = f"{_BASE_URL}/tmy"

# Default multi-year range for MRcalc (PVGIS-SARAH2 data availability)
_DEFAULT_STARTYEAR = 2005
_DEFAULT_ENDYEAR = 2020

# Default sampling lattice dimension: 5x5 = 25 points
_DEFAULT_SAMPLE_N = 5


def _annual_ghi_from_monthly(monthly: list[dict[str, Any]]) -> float:
    """Return mean annual GHI (kWh/m2/yr) from PVGIS MRcalc monthly records.

    Each record has keys ``year``, ``month``, and ``H(h)_m`` (kWh/m2/month).
    We sum per year then take the mean across years.
    """
    yearly: dict[int, float] = defaultdict(float)
    for rec in monthly:
        yearly[int(rec["year"])] += float(rec["H(h)_m"])
    if not yearly:
        raise AcquisitionError("MRcalc response contained no monthly records.")
    return float(np.mean(list(yearly.values())))


def _query_mrcalc(
    client: httpx.Client,
    lat: float,
    lon: float,
    startyear: int,
    endyear: int,
    sleep: Any = None,
) -> float:
    """Query the MRcalc endpoint and return mean annual GHI (kWh/m2/yr)."""
    _sleep = sleep if sleep is not None else _time_module.sleep
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lon,
        "startyear": startyear,
        "endyear": endyear,
        "horirrad": 1,
        "outputformat": "json",
    }
    resp = request_with_retry(client, "GET", _MRCALC_URL, params=params, sleep=_sleep)
    if resp.status_code != 200:
        raise AcquisitionError(
            f"PVGIS MRcalc returned {resp.status_code} for ({lat}, {lon}): {resp.text[:200]}"
        )
    try:
        data = resp.json()
        monthly = data["outputs"]["monthly"]
    except (KeyError, ValueError) as exc:
        raise AcquisitionError(f"Unexpected MRcalc response structure: {exc}") from exc
    return _annual_ghi_from_monthly(monthly)


def _build_sample_lons_lats(
    aoi: AOI,
    sample_n: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (lons, lats) arrays for a regular sample_n x sample_n lattice over the AOI.

    The lattice is inset by half a cell from the AOI bounds so all sample points
    are strictly inside the bounding box (avoids sea/no-data edges when the AOI
    is near the coast).
    """
    minx, miny, maxx, maxy = aoi.bounds  # WGS-84
    # Half-cell inset to avoid boundary points landing in sea/nodata
    dx = (maxx - minx) / (sample_n + 1)
    dy = (maxy - miny) / (sample_n + 1)
    lons = np.linspace(minx + dx, maxx - dx, sample_n)
    lats = np.linspace(miny + dy, maxy - dy, sample_n)
    return lons, lats


class PVGISSource(RasterSource):
    """PVGIS solar resource acquisition (monthly/annual GHI + TMY).

    Parameters
    ----------
    cache:
        Optional :class:`~solarsite.core.DiskCache` instance.  If omitted the
        default project cache (``data/cache/``) is used.
    sample_n:
        Dimension of the square sampling lattice (default 5 -> 5x5=25 points).
        Increase for larger AOIs or higher spatial-variability requirements.
    startyear, endyear:
        First/last year for the MRcalc multi-year average (PVGIS-SARAH2 spans
        2005-2020 for the Egypt region).
    """

    name = "pvgis"

    def __init__(
        self,
        cache: DiskCache | None = None,
        sample_n: int = _DEFAULT_SAMPLE_N,
        startyear: int = _DEFAULT_STARTYEAR,
        endyear: int = _DEFAULT_ENDYEAR,
    ) -> None:
        super().__init__(cache=cache)
        self.sample_n = sample_n
        self.startyear = startyear
        self.endyear = endyear

    # ------------------------------------------------------------------
    # RasterSource contract
    # ------------------------------------------------------------------

    def fetch(self, aoi: AOI, resolution_m: int = 100, **params: Any) -> xr.DataArray:
        """Wrap parent fetch to strip the non-serialisable ``_sleep`` test hook.

        ``_sleep`` must not reach the cache-key builder (which calls
        ``json.dumps``).  We pop it here and thread it through via the
        ``_fetch_uncached`` params path instead.
        """
        sleep = params.pop("_sleep", None)
        if sleep is not None:
            # Store it on the instance temporarily so _fetch_uncached can pick
            # it up even though it's invoked indirectly via the cache wrapper.
            self._test_sleep = sleep
        else:
            self._test_sleep = None
        return super().fetch(aoi, resolution_m=resolution_m, **params)

    def _fetch_uncached(
        self,
        aoi: AOI,
        resolution_m: int = 100,
        **params: Any,
    ) -> xr.DataArray:
        """Fetch annual GHI for the AOI and return it as an aligned DataArray.

        Steps:
        1. Build a ``sample_n x sample_n`` geographic lattice over the AOI.
        2. Query PVGIS MRcalc per point; collect annual GHI values.
        3. Convert sample lon/lat to working-CRS x/y.
        4. Interpolate onto the full working-CRS grid via ``griddata(linear)``
           with a ``griddata(nearest)`` fallback for any NaN pixels.
        5. Write CRS + transform via rioxarray; return with name ``"ghi_annual"``.

        Returns
        -------
        xr.DataArray
            Shape (height, width) aligned to ``grid_for_aoi(aoi, resolution_m)``,
            CRS = working UTM zone, values in **kWh/m2/year**.
        """
        # _sleep may arrive directly (when called from tests via _fetch_uncached)
        # or via self._test_sleep (when called indirectly through fetch()).
        sleep = params.pop("_sleep", None)
        if sleep is None:
            sleep = getattr(self, "_test_sleep", None)
        spec = grid_for_aoi(aoi, resolution_m)
        lons_sample, lats_sample = _build_sample_lons_lats(aoi, self.sample_n)

        # Collect annual GHI for each lattice point
        ghi_points: list[float] = []
        point_lons: list[float] = []
        point_lats: list[float] = []

        with httpx.Client(timeout=60.0) as client:
            for lat in lats_sample:
                for lon in lons_sample:
                    try:
                        ghi = _query_mrcalc(
                            client,
                            lat=float(lat),
                            lon=float(lon),
                            startyear=self.startyear,
                            endyear=self.endyear,
                            sleep=sleep,
                        )
                        ghi_points.append(ghi)
                        point_lons.append(float(lon))
                        point_lats.append(float(lat))
                        log.debug("PVGIS MRcalc (%.4f, %.4f) -> %.1f kWh/m2/yr", lat, lon, ghi)
                    except AcquisitionError as exc:
                        # Skip over-sea / no-data points; warn and continue
                        log.warning("Skipping PVGIS point (%.4f, %.4f): %s", lat, lon, exc)

        if len(ghi_points) < 3:
            raise AcquisitionError(
                f"Too few valid PVGIS sample points ({len(ghi_points)}) to interpolate. "
                "Check that the AOI contains land area covered by PVGIS-SARAH2."
            )

        # Project sample points to working CRS for interpolation
        transformer = Transformer.from_crs("EPSG:4326", spec.crs, always_xy=True)
        xs_sample, ys_sample = transformer.transform(point_lons, point_lats)

        # Build full grid coordinate arrays (cell centres)
        da_empty = empty_dataarray(spec, name="ghi_annual")
        xs_grid = da_empty.coords["x"].values  # shape (width,)
        ys_grid = da_empty.coords["y"].values  # shape (height,)
        xx_grid, yy_grid = np.meshgrid(xs_grid, ys_grid)  # (height, width) each

        points_xy = np.column_stack([xs_sample, ys_sample])  # (N, 2)
        values = np.array(ghi_points)  # (N,)
        grid_shape = (spec.height, spec.width)
        xi = np.column_stack([xx_grid.ravel(), yy_grid.ravel()])  # (H*W, 2)

        # Linear interpolation
        ghi_grid = griddata(points_xy, values, xi, method="linear").reshape(grid_shape)

        # Nearest-neighbour fallback for NaN pixels (outside convex hull)
        nan_mask = np.isnan(ghi_grid)
        if nan_mask.any():
            ghi_nearest = griddata(points_xy, values, xi, method="nearest").reshape(grid_shape)
            ghi_grid[nan_mask] = ghi_nearest[nan_mask]

        da = da_empty.copy(data=ghi_grid)
        return da

    # ------------------------------------------------------------------
    # Monthly GHI accessor
    # ------------------------------------------------------------------

    def fetch_monthly_ghi(
        self,
        aoi: AOI,
        resolution_m: int = 100,
        **params: Any,
    ) -> xr.DataArray:
        """Return a 12-band DataArray of **mean monthly GHI** (kWh/m2/month).

        The AOI centroid is queried once; the 12 long-term monthly means are
        broadcast as a constant spatial field across the grid.  This is
        sufficient for the monthly seasonality that feeds pvlib (P2.4).

        Returns
        -------
        xr.DataArray
            Shape (12, height, width), dim ``month`` = 1..12,
            values in kWh/m2/month.
        """
        sleep = params.pop("_sleep", None)
        _sleep = sleep if sleep is not None else _time_module.sleep
        spec = grid_for_aoi(aoi, resolution_m)
        centroid = aoi.geometry.centroid
        lat, lon = centroid.y, centroid.x

        api_params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "startyear": self.startyear,
            "endyear": self.endyear,
            "horirrad": 1,
            "outputformat": "json",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = request_with_retry(client, "GET", _MRCALC_URL, params=api_params, sleep=_sleep)

        if resp.status_code != 200:
            raise AcquisitionError(
                f"PVGIS MRcalc (monthly) returned {resp.status_code}: {resp.text[:200]}"
            )

        monthly_records = resp.json()["outputs"]["monthly"]

        # Compute long-term mean for each calendar month (1-12)
        month_sums: dict[int, list[float]] = defaultdict(list)
        for rec in monthly_records:
            month_sums[int(rec["month"])].append(float(rec["H(h)_m"]))

        monthly_means = np.array(
            [np.mean(month_sums[m]) for m in range(1, 13)], dtype=np.float64
        )  # shape (12,)

        da_empty = empty_dataarray(spec, name="ghi_monthly")
        # Broadcast: each month is a spatially constant field
        data_3d = np.broadcast_to(
            monthly_means[:, np.newaxis, np.newaxis],
            (12, spec.height, spec.width),
        ).copy()

        da = xr.DataArray(
            data_3d,
            dims=["month", "y", "x"],
            coords={
                "month": np.arange(1, 13, dtype=int),
                "y": da_empty.coords["y"].values,
                "x": da_empty.coords["x"].values,
            },
            name="ghi_monthly",
        )
        da = da.rio.write_crs(spec.crs, inplace=True)
        return da

    # ------------------------------------------------------------------
    # TMY endpoint
    # ------------------------------------------------------------------

    def fetch_tmy(
        self,
        aoi: AOI,
        **params: Any,
    ) -> pd.DataFrame:
        """Fetch the Typical Meteorological Year (TMY) for the AOI centroid.

        Queries the PVGIS ``tmy`` endpoint for the AOI centroid and returns
        the hourly TMY data as a DataFrame suitable for pvlib.  The result
        is cached via the standard :class:`~solarsite.core.DiskCache`
        mechanism by wrapping it in an xr.DataArray.

        .. note::
            This method bypasses the ``_fetch_uncached`` / ``fetch`` pattern
            (which returns a DataArray) and instead calls the cache directly
            so it can return a DataFrame.  Cache key uses
            ``source="pvgis_tmy"``.

        Returns
        -------
        pd.DataFrame
            Hourly TMY data.  Columns include ``G(h)`` (W/m2, GHI),
            ``T2m`` (degrees C), ``WS10m`` (m/s), and others from PVGIS.
            The index is a pandas DatetimeIndex (UTC) named ``time_utc``.
        """
        sleep = params.pop("_sleep", None)
        _sleep = sleep if sleep is not None else _time_module.sleep
        centroid = aoi.geometry.centroid
        lat, lon = centroid.y, centroid.x

        cache_key_params: dict[str, object] = {
            "resolution_m": 0,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
        }

        # We store the TMY DataFrame as a DataArray in the disk cache.
        def _compute() -> xr.DataArray:
            with httpx.Client(timeout=120.0) as client:
                api_params: dict[str, Any] = {
                    "lat": lat,
                    "lon": lon,
                    "outputformat": "json",
                }
                resp = request_with_retry(client, "GET", _TMY_URL, params=api_params, sleep=_sleep)
            if resp.status_code != 200:
                raise AcquisitionError(f"PVGIS TMY returned {resp.status_code}: {resp.text[:200]}")
            hourly = resp.json()["outputs"]["tmy_hourly"]
            df = _tmy_to_dataframe(hourly)
            return _df_to_dataarray(df)

        cached_da = self._cache.get_or_compute(
            source="pvgis_tmy",
            aoi_hash=aoi.hash,
            params=cache_key_params,
            compute_fn=_compute,
        )
        return _dataarray_to_df(cached_da)


# ------------------------------------------------------------------
# TMY helpers
# ------------------------------------------------------------------


def _tmy_to_dataframe(hourly: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert PVGIS TMY hourly list to a pandas DataFrame with DatetimeIndex."""
    df = pd.DataFrame(hourly)
    # Parse the 'time(UTC)' column: format "YYYYMMDDhhmm" (e.g. "20200101:0000")
    time_strs = df["time(UTC)"].str.replace(":", "", regex=False)
    df.index = pd.to_datetime(time_strs, format="%Y%m%d%H%M", utc=True)
    df.index.name = "time_utc"
    df = df.drop(columns=["time(UTC)"])
    return df


def _df_to_dataarray(df: pd.DataFrame) -> xr.DataArray:
    """Encode a TMY DataFrame as an xr.DataArray for caching via DiskCache.

    Layout: shape (n_hours, n_cols), coords hours=[0..n-1], cols=[colnames].
    """
    col_names = list(df.columns)
    n_hours = len(df)
    data = df.values.astype(np.float64)  # (n_hours, n_cols)
    # Store time as seconds-since-epoch in the 'hours' coord
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    time_secs = ((df.index - epoch).total_seconds()).values.astype(np.float64)

    da = xr.DataArray(
        data,
        dims=["hour", "col"],
        coords={
            "hour": np.arange(n_hours, dtype=int),
            "col": col_names,
            "time_secs": ("hour", time_secs),
        },
        name="tmy",
    )
    return da


def _dataarray_to_df(da: xr.DataArray) -> pd.DataFrame:
    """Decode a cached DataArray back to a TMY DataFrame."""
    col_names = list(da.coords["col"].values)
    time_secs = da.coords["time_secs"].values.astype(np.float64)
    index = pd.to_datetime(time_secs, unit="s", utc=True)
    index.name = "time_utc"
    df = pd.DataFrame(da.values, columns=col_names, index=index)
    return df
