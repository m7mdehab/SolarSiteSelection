"""Terrain acquisition: DEM mosaic -> elevation, slope, aspect_class.

Data source
-----------
PRIMARY  -- Copernicus GLO-30 public AWS bucket (no key required).
            URL scheme::

                https://copernicus-dem-30m.s3.amazonaws.com/
                Copernicus_DSM_COG_10_{lat_tag}_00_{lon_tag}_00_DEM/
                Copernicus_DSM_COG_10_{lat_tag}_00_{lon_tag}_00_DEM.tif

            where ``lat_tag`` = "N{dd:02d}" or "S{dd:02d}" (integer degrees of
            the SW corner) and ``lon_tag`` = "E{ddd:03d}" or "W{ddd:03d}".
            Tiles are 1-degree x 1-degree GeoTIFFs in WGS-84 with
            1-arcsecond (~30 m) resolution, available anonymously.

SECONDARY -- OpenTopography globaldem API (requires OPENTOPO_KEY env var).
            Enabled by passing ``source="opentopo"`` to ``fetch()``, or
            automatically if all GLO-30 tiles return 404 (ocean/no-data areas).

Band layout
-----------
The returned DataArray has dims ``(band, y, x)`` with the coordinate
``band = ["elevation", "slope", "aspect_class"]``::

  band 0 -- elevation     : float32, metres above sea level.
  band 1 -- slope         : float32, degrees in [0, 90].
  band 2 -- aspect_class  : float32 (integer values 0-8, stored as float32
                             for NetCDF compatibility). Compass class; see
                             mapping below.

Slope formula
-------------
Given the projected (metric) grid with cell spacing *res* metres::

    dz/dx = central-difference along the east-west axis
    dz/dy = central-difference along the north-south axis (physical, northward+)
    slope_rad = atan( sqrt( (dz/dx)**2 + (dz/dy)**2 ) )
    slope_deg = slope_rad * (180 / pi)

``numpy.gradient`` is used, which applies central differences for interior
cells and one-sided differences at boundaries (no special edge treatment).

Because raster row index increases *southward*, the physical northward gradient
equals the *negative* of ``numpy.gradient``'s row-wise output::

    dz/dy_physical = -grad_rows / res_m

Aspect-class mapping
--------------------
Aspect is the direction of steepest *descent* (downslope direction), measured
clockwise from North.  The mapping to 8-way compass classes follows
``configs/criteria.yaml``::

    0  -> flat         (slope < 0.5 deg)
    1  -> north        [337.5, 360) union [0, 22.5)
    2  -> northeast    [22.5, 67.5)
    3  -> east         [67.5, 112.5)
    4  -> southeast    [112.5, 157.5)
    5  -> south        [157.5, 202.5)
    6  -> southwest    [202.5, 247.5)
    7  -> west         [247.5, 292.5)
    8  -> northwest    [292.5, 337.5)

``criteria.yaml`` aspect labels: flat=0, north=1, northeast=2, east=3,
southeast=4, south=5, southwest=6, west=7, northwest=8.

NetCDF / cache compatibility
----------------------------
``rioxarray.write_crs()`` attaches a ``spatial_ref`` scalar coordinate that
serialises as a separate NetCDF variable, which causes ``xr.open_dataarray``
to fail (it requires exactly one data variable).  To keep the DataArray
compatible with the project's DiskCache, CRS metadata is stored as a
0-dimensional ``spatial_ref`` coordinate (CF convention) using
``xr.Variable([], 0, attrs={...})``.  The coordinate is not a data variable,
so ``open_dataarray`` succeeds, and rioxarray still reads ``rio.crs`` from it.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
from typing import Any

import httpx
import numpy as np
import rasterio
import rioxarray  # noqa: F401  -- activates .rio accessor on xr.DataArray
import xarray as xr
from dotenv import load_dotenv
from rasterio.merge import merge as rio_merge

from solarsite.acquire.base import AcquisitionError, RasterSource, grid_for_aoi, request_with_retry
from solarsite.core import AOI, GridSpec

__all__ = ["TerrainSource"]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Slope threshold below which a cell is classified as "flat" (aspect_class = 0).
_FLAT_THRESHOLD_DEG = 0.5

#: Copernicus GLO-30 public AWS base URL (anonymous access, no key required).
_GLO30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

#: OpenTopography globaldem API endpoint.
_OPENTOPO_BASE = "https://portal.opentopography.org/API/globaldem"

#: Nodata sentinel used for the intermediate mosaic before reprojection.
_NODATA = -9999.0


# ---------------------------------------------------------------------------
# Tile URL helpers
# ---------------------------------------------------------------------------


def _glo30_tile_url(lat: int, lon: int) -> str:
    """Return the GLO-30 GeoTIFF URL for the 1-degree tile whose SW corner is (lat, lon).

    Both *lat* and *lon* are integer degrees (floor of WGS-84 coordinate).

    URL pattern::

        {BASE}/Copernicus_DSM_COG_10_{lat_tag}_00_{lon_tag}_00_DEM/
               Copernicus_DSM_COG_10_{lat_tag}_00_{lon_tag}_00_DEM.tif
    """
    lat_tag = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    lon_tag = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    stem = f"Copernicus_DSM_COG_10_{lat_tag}_00_{lon_tag}_00_DEM"
    return f"{_GLO30_BASE}/{stem}/{stem}.tif"


def _tiles_for_aoi(bounds_wgs84: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """Return list of (lat, lon) SW-corner integer tuples covering AOI bounds.

    ``bounds_wgs84`` is ``(minx, miny, maxx, maxy)`` in WGS-84 decimal degrees.
    """
    minx, miny, maxx, maxy = bounds_wgs84
    tiles = []
    for lat in range(math.floor(miny), math.ceil(maxy)):
        for lon in range(math.floor(minx), math.ceil(maxx)):
            tiles.append((lat, lon))
    return tiles


# ---------------------------------------------------------------------------
# Slope and aspect computation
# ---------------------------------------------------------------------------


def _compute_slope_aspect(
    elevation_arr: np.ndarray,
    res_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute slope (degrees) and aspect class from a 2-D elevation array.

    Parameters
    ----------
    elevation_arr:
        2-D float array of elevation values in metres.  Row 0 is the
        *northernmost* row (standard raster top-to-bottom convention).
    res_m:
        Cell spacing in metres (assumed equal in both x and y).

    Returns
    -------
    slope_deg : 2-D float32 array, range [0, 90 deg].
    aspect_class : 2-D int16 array, values 0-8 per module docstring.

    Notes
    -----
    ``numpy.gradient`` returns *[grad_rows, grad_cols]* where each element is
    the gradient of elevation with respect to the array *index*.  Because row
    index increases *southward*, the physically northward gradient is::

        dzdy_north = -grad_rows / res_m

    The east-west gradient is::

        dzdx_east = grad_cols / res_m

    Slope is the magnitude of the gradient vector::

        slope_deg = degrees( atan( sqrt(dzdx**2 + dzdy**2) ) )

    Aspect (direction of steepest *descent*, CW from North)::

        compass_deg = degrees( atan2(-dzdx_east, -dzdy_north) ) mod 360

    Verification:
      * South-facing (dzdy_north > 0, dzdx = 0):
        atan2(0, -positive) = 180 deg => south class 5 confirmed
      * North-facing (dzdy_north < 0, dzdx = 0):
        atan2(0, positive) = 0 deg => north class 1 confirmed
      * East-facing  (dzdx < 0 -- elev drops eastward):
        atan2(positive, 0) = 90 deg => east class 3 confirmed
    """
    arr = elevation_arr.astype(np.float64)

    # numpy.gradient returns [grad_rows, grad_cols]; both in elevation/index units.
    grad_rows, grad_cols = np.gradient(arr, res_m)

    dzdx = grad_cols  # east-west physical gradient (positive = uphill east)
    dzdy_north = -grad_rows  # northward physical gradient (positive = uphill north)

    slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy_north**2))
    slope_deg = np.degrees(slope_rad).astype(np.float32)

    # Aspect: compass bearing of steepest descent = atan2(-dzdx, -dzdy_north) mod 360.
    compass_deg = (np.degrees(np.arctan2(-dzdx, -dzdy_north)) + 360.0) % 360.0

    aspect_class = np.zeros(elevation_arr.shape, dtype=np.int16)
    is_sloped = slope_deg >= _FLAT_THRESHOLD_DEG

    aspect_class[is_sloped & ((compass_deg >= 337.5) | (compass_deg < 22.5))] = 1  # N
    aspect_class[is_sloped & (compass_deg >= 22.5) & (compass_deg < 67.5)] = 2  # NE
    aspect_class[is_sloped & (compass_deg >= 67.5) & (compass_deg < 112.5)] = 3  # E
    aspect_class[is_sloped & (compass_deg >= 112.5) & (compass_deg < 157.5)] = 4  # SE
    aspect_class[is_sloped & (compass_deg >= 157.5) & (compass_deg < 202.5)] = 5  # S
    aspect_class[is_sloped & (compass_deg >= 202.5) & (compass_deg < 247.5)] = 6  # SW
    aspect_class[is_sloped & (compass_deg >= 247.5) & (compass_deg < 292.5)] = 7  # W
    aspect_class[is_sloped & (compass_deg >= 292.5) & (compass_deg < 337.5)] = 8  # NW

    return slope_deg, aspect_class


# ---------------------------------------------------------------------------
# CRS serialisation helper (CF-compliant, DiskCache-compatible)
# ---------------------------------------------------------------------------


def _write_crs_cf(da: xr.DataArray, crs: Any) -> xr.DataArray:
    """Attach CRS metadata to *da* in a way that survives a NetCDF round-trip.

    ``rioxarray.write_crs()`` adds a ``spatial_ref`` *scalar coordinate* whose
    value is 0 and whose attributes carry the CRS WKT.  When saved to NetCDF,
    this scalar coordinate becomes a *data variable*, causing
    ``xr.open_dataarray`` to fail (it requires exactly one data variable).

    This helper creates the same CF-compliant ``spatial_ref`` coordinate
    directly via ``xr.Variable``, which xarray serialises as a coordinate
    (not a variable).  After reload, ``da.rio.crs`` reads the CRS correctly.

    Parameters
    ----------
    da:
        DataArray to annotate (NOT modified in-place).
    crs:
        A pyproj or rasterio CRS object.

    Returns
    -------
    A new DataArray with ``spatial_ref`` as a 0-dimensional coordinate and
    ``grid_mapping = "spatial_ref"`` in ``attrs``.
    """
    from pyproj import CRS as ProjCRS

    if not isinstance(crs, ProjCRS):
        crs = ProjCRS.from_user_input(crs)

    crs_wkt = crs.to_wkt()
    spatial_ref_var = xr.Variable(
        [],
        0,
        attrs={
            "crs_wkt": crs_wkt,
            "spatial_ref": crs_wkt,
        },
    )
    result = da.assign_coords(spatial_ref=spatial_ref_var)
    result.attrs["grid_mapping"] = "spatial_ref"
    return result


# ---------------------------------------------------------------------------
# TerrainSource
# ---------------------------------------------------------------------------


class TerrainSource(RasterSource):
    """Terrain data source: DEM -> elevation, slope, aspect_class.

    Fetches Copernicus GLO-30 tiles from the public AWS bucket (no key needed).
    Falls back to OpenTopography if GLO-30 fails or ``source="opentopo"`` is
    passed.  Results are aligned to the AOI working-CRS grid.

    Parameters
    ----------
    cache:
        Optional DiskCache instance; a default cache is created if omitted.
    """

    name = "terrain"

    def _fetch_uncached(
        self,
        aoi: AOI,
        resolution_m: int = 100,
        *,
        source: str = "glo30",
        **params: Any,
    ) -> xr.DataArray:
        """Fetch terrain bands and return a (band, y, x) DataArray.

        Parameters
        ----------
        aoi:
            Area of interest (WGS-84 geometry + bounds).
        resolution_m:
            Target raster resolution in metres.
        source:
            ``"glo30"`` (default) -- Copernicus AWS primary source.
            ``"opentopo"`` -- OpenTopography API (requires OPENTOPO_KEY).
            GLO-30 also falls back to OpenTopography on AcquisitionError.

        Returns
        -------
        xr.DataArray with:
          * dims ``(band, y, x)``
          * band coordinate ``["elevation", "slope", "aspect_class"]``
          * CRS metadata written as a CF-compliant ``spatial_ref`` coordinate
          * transform stored in ``attrs["transform"]``
        """
        grid = grid_for_aoi(aoi, resolution_m)

        if source == "opentopo":
            elev_da = self._fetch_opentopo(aoi, grid)
        else:
            try:
                elev_da = self._fetch_glo30(aoi, grid)
            except AcquisitionError:
                log.warning("GLO-30 fetch failed, falling back to OpenTopography.")
                elev_da = self._fetch_opentopo(aoi, grid)

        # elev_da is (y, x) in the working CRS, aligned to grid.
        elev_raw = elev_da.values.astype(np.float32)
        nodata_mask = np.isnan(elev_raw)

        # Fill NaN cells with the array median before computing gradient-based
        # slope/aspect so that edge cells do not produce artefacts from the
        # 0-fill that nan_to_num would introduce.  The NaN positions are
        # restored in the output after slope/aspect are computed.
        if nodata_mask.any():
            fill_val = float(np.nanmedian(elev_raw)) if not np.all(nodata_mask) else 0.0
            elev_filled = np.where(nodata_mask, fill_val, elev_raw)
        else:
            elev_filled = elev_raw

        slope_np, aspect_np = _compute_slope_aspect(elev_filled, float(resolution_m))

        # Restore NaN where elevation was missing.
        if nodata_mask.any():
            slope_np = np.where(nodata_mask, np.nan, slope_np)
            aspect_np = np.where(nodata_mask, 0, aspect_np).astype(np.int16)

        elev_np = np.where(nodata_mask, np.nan, elev_raw)

        stacked = np.stack(
            [elev_np, slope_np, aspect_np.astype(np.float32)],
            axis=0,
        )

        da = xr.DataArray(
            stacked,
            dims=["band", "y", "x"],
            coords={
                "band": ["elevation", "slope", "aspect_class"],
                "y": elev_da.coords["y"].values,
                "x": elev_da.coords["x"].values,
            },
            name="terrain",
        )

        # Store transform in attrs for callers that need it
        t = grid.transform
        da.attrs["transform"] = [t.a, t.b, t.c, t.d, t.e, t.f]

        # Attach CRS using the CF-convention approach that survives the cache round-trip.
        da = _write_crs_cf(da, grid.crs)
        return da

    # ------------------------------------------------------------------
    # GLO-30 primary path
    # ------------------------------------------------------------------

    def _fetch_glo30(self, aoi: AOI, grid: GridSpec) -> xr.DataArray:
        """Fetch and mosaic Copernicus GLO-30 tiles covering *aoi*.

        Returns a (y, x) float32 DataArray in the working CRS aligned to *grid*.
        404 responses (ocean / missing tiles) are silently skipped.  Raises
        :class:`~solarsite.acquire.base.AcquisitionError` if all tiles return
        404 or a non-200 status.
        """
        tiles = _tiles_for_aoi(aoi.bounds)
        if not tiles:
            raise AcquisitionError("No GLO-30 tiles computed for AOI bounds.")

        datasets: list[rasterio.DatasetReader] = []
        with httpx.Client(timeout=60.0) as client:
            for lat, lon in tiles:
                url = _glo30_tile_url(lat, lon)
                log.debug("Fetching GLO-30 tile: %s", url)
                resp = request_with_retry(client, "GET", url)
                if resp.status_code == 404:
                    log.debug("GLO-30 tile 404 (ocean/no-data): %s", url)
                    continue
                if resp.status_code != 200:
                    raise AcquisitionError(f"GLO-30 tile {url} returned HTTP {resp.status_code}")
                buf = io.BytesIO(resp.content)
                buf.name = "tile.tif"
                datasets.append(rasterio.open(buf))

        if not datasets:
            raise AcquisitionError("All GLO-30 tile fetches returned 404 (AOI may be ocean-only).")

        return self._mosaic_and_reproject(datasets, grid)

    # ------------------------------------------------------------------
    # OpenTopography secondary path
    # ------------------------------------------------------------------

    def _fetch_opentopo(self, aoi: AOI, grid: GridSpec) -> xr.DataArray:
        """Fetch a DEM from the OpenTopography globaldem API.

        Requires ``OPENTOPO_KEY`` in the environment (loaded via python-dotenv
        from ``.env`` at the project root).

        Parameters
        ----------
        aoi:
            AOI whose WGS-84 bounds define the request extent.
        grid:
            Target grid for reprojection/alignment.

        Raises
        ------
        AcquisitionError:
            If the key is missing or the API returns a non-200 response.
        """
        load_dotenv()
        api_key = os.environ.get("OPENTOPO_KEY", "")
        if not api_key:
            raise AcquisitionError(
                "OPENTOPO_KEY not set; cannot use OpenTopography secondary source."
            )

        minx, miny, maxx, maxy = aoi.bounds
        req_params = {
            "demtype": "COP30",
            "south": str(miny),
            "north": str(maxy),
            "west": str(minx),
            "east": str(maxx),
            "outputFormat": "GTiff",
            "API_Key": api_key,
        }
        with httpx.Client(timeout=120.0) as client:
            resp = request_with_retry(client, "GET", _OPENTOPO_BASE, params=req_params)
        if resp.status_code != 200:
            raise AcquisitionError(
                f"OpenTopography returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        buf = io.BytesIO(resp.content)
        buf.name = "dem_opentopo.tif"
        ds = rasterio.open(buf)
        return self._mosaic_and_reproject([ds], grid)

    # ------------------------------------------------------------------
    # Shared mosaic + reproject helper
    # ------------------------------------------------------------------

    def _mosaic_and_reproject(
        self,
        datasets: list[rasterio.DatasetReader],
        grid: GridSpec,
    ) -> xr.DataArray:
        """Mosaic *datasets*, reproject to *grid*, return aligned (y, x) DataArray.

        The mosaic is built in WGS-84 (source CRS of GLO-30 / OpenTopography),
        then reprojected to the working UTM CRS of *grid*.  Nodata sentinel
        values (_NODATA = -9999) are masked to NaN after reprojection.
        """
        mosaic_arr, mosaic_transform = rio_merge(datasets, method="first", nodata=_NODATA)
        mosaic_band = mosaic_arr[0].astype(np.float32)
        src_crs = datasets[0].crs

        for ds in datasets:
            with contextlib.suppress(Exception):
                ds.close()

        height, width = mosaic_band.shape
        left = mosaic_transform.c
        top = mosaic_transform.f
        dx = mosaic_transform.a
        dy = mosaic_transform.e  # negative (north-up)

        x_coords = left + (np.arange(width) + 0.5) * dx
        y_coords = top + (np.arange(height) + 0.5) * dy

        raw_da = xr.DataArray(
            mosaic_band,
            dims=["y", "x"],
            coords={"y": y_coords, "x": x_coords},
            name="elevation",
        )
        raw_da = raw_da.rio.write_crs(src_crs, inplace=True)
        raw_da = raw_da.rio.write_nodata(_NODATA, inplace=True)

        from solarsite.core.raster import reproject_to_grid

        aligned: xr.DataArray = reproject_to_grid(raw_da, grid, resampling="bilinear")

        # Replace the nodata sentinel with NaN so slope/aspect calculations
        # are not corrupted by the -9999 fill value at tile edges.
        aligned = aligned.where(aligned != _NODATA)
        return aligned
