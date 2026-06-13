"""Climate data acquisition via the Open-Meteo Archive API.

Subphase P1.5 - Climate (Open-Meteo archive API).

Produces a 3-band raster (temperature, humidity, wind_speed) by:

1. Sampling a coarse NxN lattice over the AOI bounding box in geographic
   coordinates (default density: <=~25 points per 10,000 km2; exact point count
   is ``ceil(sqrt(area_km2 / 10_000.0 * max_points))`` clamped to 2..MAX_PTS).
2. Querying the Open-Meteo Archive API for each lattice point, fetching full-year
   hourly temperature_2m (deg C), relative_humidity_2m (%), and wind_speed_10m (km/h)
   for a recent full calendar year (default 2023).
3. Computing the annual mean of each variable per point (simple arithmetic mean
   of all non-NaN hourly values). Wind speed is converted from km/h to m/s.
4. Interpolating the scattered point means onto the working-CRS grid using
   scipy.interpolate.griddata with method="linear" for interior cells, then
   filling remaining NaN cells with method="nearest" (extrapolation fallback).

Band layout (``band`` coordinate, dim 0):
  - ``"temperature"`` - annual mean 2 m air temperature (deg C)
  - ``"humidity"``    - annual mean 2 m relative humidity (%)
  - ``"wind_speed"``  - annual mean 10 m wind speed (m/s)

All HTTP requests use :func:`solarsite.acquire.base.request_with_retry` so
transient 429/5xx failures back off and retry up to 4 times.

API endpoint: https://archive-api.open-meteo.com/v1/archive
Parameters used per call:
  latitude=<lat>&longitude=<lon>
  &start_date=<YEAR>-01-01&end_date=<YEAR>-12-31
  &hourly=temperature_2m,relative_humidity_2m,wind_speed_10m
  &wind_speed_unit=ms          (request m/s directly from API)
  &timezone=UTC
No API key required.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx
import numpy as np
import rioxarray  # noqa: F401  — registers the .rio accessor on xr.DataArray
import xarray as xr
from pyproj import Transformer
from scipy.interpolate import griddata

from solarsite.core import AOI, empty_dataarray
from solarsite.core.grid import DEFAULT_RESOLUTION_M

from .base import RasterSource, grid_for_aoi, request_with_retry

__all__ = [
    "ClimateSource",
    "wind_hybrid_layer",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_DEFAULT_YEAR = 2023
_KMH_TO_MS = 1.0 / 3.6  # km/h to m/s conversion factor

# Sampling density: at most this many points for a 10,000 km2 AOI.
# For smaller AOIs the count scales proportionally (sqrt of area ratio).
# Clamped to [2, MAX_SAMPLE_PTS] to ensure at least a 2x2 grid for interpolation.
_MAX_SAMPLE_PTS = 25
_MIN_SAMPLE_PTS = 2

BAND_NAMES = ["temperature", "humidity", "wind_speed"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_point_count(area_km2: float, max_pts: int = _MAX_SAMPLE_PTS) -> int:
    """Return the lattice side length N so N**2 points cover the AOI.

    Density is proportional to sqrt(area / 10_000) clamped to [MIN, MAX].
    For the reference 10,000 km2 AOI this yields max_pts lattice points;
    for smaller AOIs it scales down so very small AOIs still get a few points.
    """
    pts = math.ceil(math.sqrt(max(area_km2, 0.0) / 10_000.0) * max_pts)
    return max(_MIN_SAMPLE_PTS, min(pts, max_pts))


def _lattice_points(
    bounds: tuple[float, float, float, float],
    n: int,
) -> list[tuple[float, float]]:
    """Generate an NxN regular lattice of (lon, lat) points inside *bounds*.

    Args:
        bounds: (minx, miny, maxx, maxy) in WGS-84.
        n: Number of grid lines along each axis; produces n**2 points total.

    Returns:
        List of (lon, lat) tuples, left-to-right, bottom-to-top.
    """
    minx, miny, maxx, maxy = bounds
    # Place points at cell centres of an N-column x N-row grid.
    lon_step = (maxx - minx) / n
    lat_step = (maxy - miny) / n
    pts = []
    for j in range(n):
        lat = miny + (j + 0.5) * lat_step
        for i in range(n):
            lon = minx + (i + 0.5) * lon_step
            pts.append((lon, lat))
    return pts


def _fetch_point(
    client: httpx.Client,
    lat: float,
    lon: float,
    year: int,
    sleep: Any = None,
) -> dict[str, Any]:
    """Fetch one year of hourly climate data for a single (lat, lon) point.

    Returns the parsed JSON response dict from Open-Meteo Archive.
    """
    params: dict[str, str] = {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }
    kwargs: dict[str, Any] = {"params": params}
    if sleep is not None:
        kwargs["sleep"] = sleep
    resp = request_with_retry(client, "GET", _ARCHIVE_URL, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _annual_mean(values: list[float | int | None]) -> float:
    """Return the arithmetic mean of non-null values; raises ValueError if all null."""
    arr = np.array([v for v in values if v is not None], dtype=np.float64)
    if arr.size == 0:
        raise ValueError("All values are null; cannot compute annual mean.")
    return float(np.nanmean(arr))


def _interpolate_to_grid(
    spec: Any,
    lons: np.ndarray,
    lats: np.ndarray,
    values: np.ndarray,
) -> np.ndarray:
    """Interpolate scattered (lon, lat, value) points onto the working-CRS grid.

    Strategy:
      1. Convert scattered WGS-84 (lon, lat) to working-CRS (x, y) coordinates.
      2. Use scipy griddata with method="linear" - exact at sample points,
         Delaunay-triangulated linear interpolation between them.
      3. Fill any NaN cells (extrapolation gaps) with method="nearest".

    Args:
        spec: Working-CRS GridSpec.
        lons: 1-D array of sample point longitudes (WGS-84).
        lats: 1-D array of sample point latitudes (WGS-84).
        values: 1-D array of per-point annual mean values.

    Returns:
        2-D numpy array of shape (height, width) with interpolated values.
    """
    transformer = Transformer.from_crs("EPSG:4326", spec.crs, always_xy=True)
    xs, ys = transformer.transform(lons, lats)
    pts_proj = np.column_stack([xs, ys])

    # Grid cell-centre coordinates in the working CRS.
    gx = spec.minx + (np.arange(spec.width) + 0.5) * spec.resolution_m
    gy = spec.maxy - (np.arange(spec.height) + 0.5) * spec.resolution_m
    grid_x, grid_y = np.meshgrid(gx, gy)

    # Linear interpolation (Delaunay triangulation); NaN outside convex hull.
    grid_vals = griddata(pts_proj, values, (grid_x, grid_y), method="linear")

    # Nearest-neighbour fallback for any NaN cells (e.g. corners outside hull).
    mask = np.isnan(grid_vals)
    if mask.any():
        grid_nearest = griddata(pts_proj, values, (grid_x, grid_y), method="nearest")
        grid_vals = np.where(mask, grid_nearest, grid_vals)

    return grid_vals  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Main source class
# ---------------------------------------------------------------------------


class ClimateSource(RasterSource):
    """Raster source for annual mean temperature, humidity, and wind speed.

    Uses the Open-Meteo Archive API (no API key required) to build a 3-band
    raster aligned to the working-CRS grid.  Point-sampling density scales
    with AOI area (up to ``max_sample_pts`` for a 10,000 km2 AOI); values are
    interpolated onto the full grid via scipy griddata.

    Parameters
    ----------
    cache:
        Optional :class:`~solarsite.core.DiskCache` to avoid repeated downloads.
    max_sample_pts:
        Maximum number of lattice points for a 10,000 km2 AOI.  Actual count
        is ``ceil(sqrt(area_km2 / 10_000) * max_sample_pts)``.
    year:
        Full calendar year to fetch from the archive (default 2023).
    """

    name = "openmeteo"

    def __init__(
        self,
        cache: Any = None,
        max_sample_pts: int = _MAX_SAMPLE_PTS,
        year: int = _DEFAULT_YEAR,
    ) -> None:
        super().__init__(cache=cache)
        self.max_sample_pts = max_sample_pts
        self.year = year

    # ------------------------------------------------------------------
    # Core implementation
    # ------------------------------------------------------------------

    def _fetch_uncached(
        self,
        aoi: AOI,
        resolution_m: int = DEFAULT_RESOLUTION_M,
        **params: Any,
    ) -> xr.DataArray:
        """Fetch climate raster for *aoi* at *resolution_m*.

        Returns an xr.DataArray with dims ``(band, y, x)``, ``band`` coordinate
        ``["temperature", "humidity", "wind_speed"]``, in the working CRS.
        """
        spec = grid_for_aoi(aoi, resolution_m)

        n = _sample_point_count(aoi.area_km2, self.max_sample_pts)
        log.info(
            "ClimateSource: sampling %dx%d=%d lattice points over AOI (%.0f km2), year=%d",
            n,
            n,
            n * n,
            aoi.area_km2,
            self.year,
        )
        lattice = _lattice_points(aoi.bounds, n)

        sleep_fn = params.get("sleep")

        with httpx.Client(timeout=60.0) as client:
            band_arrays = self._sample_and_interpolate(
                client=client,
                spec=spec,
                lattice=lattice,
                sleep_fn=sleep_fn,
            )

        return self._build_dataarray(spec, band_arrays)

    def _sample_and_interpolate(
        self,
        client: httpx.Client,
        spec: Any,
        lattice: list[tuple[float, float]],
        sleep_fn: Any = None,
    ) -> dict[str, np.ndarray]:
        """Query every lattice point and interpolate each band onto the grid.

        Returns a dict mapping band name to a 2-D numpy array (height, width).
        """
        lons_list: list[float] = []
        lats_list: list[float] = []
        temps: list[float] = []
        humids: list[float] = []
        winds: list[float] = []

        for lon, lat in lattice:
            log.debug("ClimateSource: querying (%.4f, %.4f)", lat, lon)
            fetch_kwargs: dict[str, Any] = {}
            if sleep_fn is not None:
                fetch_kwargs["sleep"] = sleep_fn
            data = _fetch_point(client, lat, lon, self.year, **fetch_kwargs)

            hourly = data.get("hourly", {})
            temp_mean = _annual_mean(hourly.get("temperature_2m", []))
            humid_mean = _annual_mean(hourly.get("relative_humidity_2m", []))

            # wind_speed_unit=ms requested; some API versions may return km/h.
            # We check the units header to decide whether to convert.
            units = data.get("hourly_units", {})
            wind_unit = units.get("wind_speed_10m", "km/h")
            raw_wind = _annual_mean(hourly.get("wind_speed_10m", []))
            if "km/h" in wind_unit or "kph" in wind_unit.lower():
                wind_mean = raw_wind * _KMH_TO_MS
            else:
                # Treat as m/s (the requested unit)
                wind_mean = raw_wind

            lons_list.append(lon)
            lats_list.append(lat)
            temps.append(temp_mean)
            humids.append(humid_mean)
            winds.append(wind_mean)

        lons_arr = np.array(lons_list)
        lats_arr = np.array(lats_list)

        return {
            "temperature": _interpolate_to_grid(spec, lons_arr, lats_arr, np.array(temps)),
            "humidity": _interpolate_to_grid(spec, lons_arr, lats_arr, np.array(humids)),
            "wind_speed": _interpolate_to_grid(spec, lons_arr, lats_arr, np.array(winds)),
        }

    def _build_dataarray(
        self,
        spec: Any,
        band_arrays: dict[str, np.ndarray],
    ) -> xr.DataArray:
        """Stack per-band 2-D arrays into a (band, y, x) DataArray.

        Coordinates:
          - ``band``: ["temperature", "humidity", "wind_speed"]
          - ``y``, ``x``: cell-centre coordinates in the working CRS (metres)

        Spatial metadata (CRS, transform) written via rioxarray.
        """
        template = empty_dataarray(spec, name="climate")
        y_coords = template.coords["y"].values
        x_coords = template.coords["x"].values

        stacked = np.stack(
            [band_arrays[b] for b in BAND_NAMES],
            axis=0,
        )  # shape: (3, height, width)

        da = xr.DataArray(
            stacked,
            dims=["band", "y", "x"],
            coords={
                "band": BAND_NAMES,
                "y": y_coords,
                "x": x_coords,
            },
            name="climate",
        )
        da = da.rio.write_crs(spec.crs, inplace=True)
        da = da.rio.write_transform(spec.transform, inplace=True)
        return da


# ---------------------------------------------------------------------------
# Bonus: wind hybrid-potential layer
# ---------------------------------------------------------------------------


def wind_hybrid_layer(
    aoi: AOI,
    resolution_m: int = DEFAULT_RESOLUTION_M,
    cache: Any = None,
    year: int = _DEFAULT_YEAR,
) -> xr.DataArray:
    """Return the wind-speed band as a standalone hybrid-potential layer.

    Mirrors the legacy app's inclusion of wind speed as a 'bonus hybrid-potential'
    layer that can feed a complementary wind/solar siting analysis.

    Returns an xr.DataArray with dims ``(band, y, x)`` and ``band=["wind_speed"]``.
    The spatial extent, CRS, and resolution match the full ClimateSource raster.
    """
    source = ClimateSource(cache=cache, year=year)
    full = source.fetch(aoi, resolution_m=resolution_m)
    return full.sel(band=["wind_speed"])
