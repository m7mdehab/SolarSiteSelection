"""Tests for working-CRS policy (src/solarsite/core/crs.py)."""

from __future__ import annotations

from pyproj import CRS
from shapely.geometry import box

from solarsite.core.crs import AREA_CRS, WGS84, utm_zone_for, working_crs_for

# ---------------------------------------------------------------------------
# utm_zone_for
# ---------------------------------------------------------------------------


def test_utm_zone_egypt_eastern() -> None:
    """Cairo (~31.2°E, 30.1°N) falls in UTM zone 36N (EPSG:32636)."""
    epsg = utm_zone_for(31.2, 30.1)
    assert epsg == 32636


def test_utm_zone_egypt_western() -> None:
    """Siwa (~25.5°E, 29.2°N) falls in UTM zone 35N (EPSG:32635)."""
    epsg = utm_zone_for(25.5, 29.2)
    assert epsg == 32635


def test_utm_zone_southern_hemisphere() -> None:
    """Sydney (~151°E, -33.9°S) falls in UTM zone 56S (EPSG:32756)."""
    epsg = utm_zone_for(151.0, -33.9)
    assert epsg == 32756


def test_utm_zone_prime_meridian() -> None:
    """At exactly 0 deg longitude, UTM zone 31N starts (zone 31 spans 0-6E)."""
    # Zone 30 covers 6W to 0; zone 31 covers 0 to 6E.
    # Longitude 0 is the western boundary of zone 31 -> EPSG:32631.
    epsg = utm_zone_for(0.0, 51.5)
    assert epsg == 32631  # zone 31N


def test_utm_zone_london() -> None:
    """London (~0.1°W, 51.5°N) falls in UTM zone 30N (EPSG:32630)."""
    epsg = utm_zone_for(-0.1, 51.5)
    assert epsg == 32630


def test_utm_zone_date_line() -> None:
    """Far eastern Russia (~179.9°E) → zone 60N."""
    epsg = utm_zone_for(179.9, 60.0)
    assert epsg == 32660


def test_utm_zone_far_west() -> None:
    """Western edge (-179.9°E) → zone 1N."""
    epsg = utm_zone_for(-179.9, 45.0)
    assert epsg == 32601


def test_utm_zone_returns_int() -> None:
    epsg = utm_zone_for(30.0, 30.0)
    assert isinstance(epsg, int)


# ---------------------------------------------------------------------------
# working_crs_for
# ---------------------------------------------------------------------------


def test_working_crs_for_cairo_box() -> None:
    """Small box around Cairo → UTM 36N."""
    geom = box(30.5, 29.5, 31.5, 30.5)  # Centroid ~ (31°E, 30°N)
    crs = working_crs_for(geom)
    assert isinstance(crs, CRS)
    assert crs.to_epsg() == 32636


def test_working_crs_for_siwa_box() -> None:
    """Small box around Siwa → UTM 35N."""
    geom = box(25.0, 29.0, 26.0, 30.0)  # Centroid ~ (25.5°E, 29.5°N)
    crs = working_crs_for(geom)
    assert crs.to_epsg() == 32635


def test_working_crs_for_returns_crs_object() -> None:
    geom = box(30.0, 29.0, 31.0, 30.0)
    crs = working_crs_for(geom)
    assert isinstance(crs, CRS)


def test_working_crs_is_metric() -> None:
    """UTM CRS should use metres as the linear unit."""
    geom = box(30.0, 29.0, 31.0, 30.0)
    crs = working_crs_for(geom)
    # Axis unit name should be 'metre'
    axes = crs.axis_info
    units = {ax.unit_name for ax in axes}
    assert "metre" in units


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_area_crs_is_equal_area() -> None:
    """AREA_CRS (EPSG:6933) should be a valid projected CRS."""
    assert AREA_CRS.is_projected
    assert AREA_CRS.to_epsg() == 6933


def test_wgs84_is_geographic() -> None:
    assert WGS84.is_geographic
    assert WGS84.to_epsg() == 4326
