"""Working-CRS policy for SolarSiteSelection.

Strategy: UTM zone of the AOI centroid
---------------------------------------
For a maximum AOI of 10,000 km2 (<=~100 km x 100 km), a single UTM zone is
always a valid working projection.  UTM gives:

  * Metric units (metres) - essential for distance/resolution work.
  * Low distortion (< 0.04 % within a zone) - good for both area and distance.
  * Well-known, widely supported by all geospatial tools (EPSG:326xx / 327xx).

Egypt spans UTM zones 35N (western) and 36N (eastern).  The centroid-based
selection handles the zone boundary automatically for any worldwide AOI.

Alternatives considered
-----------------------
  * EPSG:6933 (WGS 84 / EASE-Grid 2.0) - global equal-area, but uses metres
    that are NOT locally accurate for distance measurement near the poles.
    We use it in aoi.py only for area validation.
  * Africa Albers Equal Area (ESRI:102022) - Egypt-specific, not general.
  * Web Mercator (EPSG:3857) - not equal-area, strong distortion at mid-lats.

Decision: UTM zone of the centroid, WGS-84 datum.
  EPSG = 32600 + zone  (North)
  EPSG = 32700 + zone  (South)

Rationale: metric and low-distortion (valid for AOIs up to ~10,000 km2), globally
applicable. Egypt spans UTM zones 35 N-36 N; the centroid lookup auto-selects.
"""

from __future__ import annotations

from pyproj import CRS
from shapely.geometry.base import BaseGeometry

__all__ = [
    "AREA_CRS",
    "WGS84",
    "utm_zone_for",
    "working_crs_for",
]

# Equal-area CRS used for AOI area calculation (not general raster work)
AREA_CRS: CRS = CRS.from_epsg(6933)

# Standard geographic CRS for input data
WGS84: CRS = CRS.from_epsg(4326)


def utm_zone_for(lon: float, lat: float) -> int:
    """Return the UTM EPSG code for a given longitude/latitude (WGS-84).

    Args:
        lon: Longitude in degrees, range [-180, 180].
        lat: Latitude in degrees, range [-90, 90].

    Returns:
        EPSG integer, e.g. 32636 for UTM zone 36N.
    """
    # UTM zone number (1-60), each zone is 6° wide starting at -180°
    zone_number = int((lon + 180.0) / 6.0) % 60 + 1
    if lat >= 0.0:
        return 32600 + zone_number  # Northern hemisphere
    else:
        return 32700 + zone_number  # Southern hemisphere


def working_crs_for(geometry: BaseGeometry) -> CRS:
    """Return the working CRS (UTM) for the given WGS-84 geometry.

    Picks the UTM zone that contains the centroid of the geometry.

    Args:
        geometry: A shapely geometry in WGS-84 (EPSG:4326) coordinates.

    Returns:
        A pyproj CRS object for the appropriate UTM zone.
    """
    centroid = geometry.centroid
    epsg = utm_zone_for(centroid.x, centroid.y)
    return CRS.from_epsg(epsg)
