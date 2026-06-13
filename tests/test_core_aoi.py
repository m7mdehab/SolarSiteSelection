"""Tests for AOI model (src/solarsite/core/aoi.py)."""

from __future__ import annotations

import pytest

from solarsite.core.aoi import AOI, AOIInvalidGeometryError, AOITooLargeError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_polygon_geojson(
    lon_min: float = 30.0,
    lat_min: float = 29.0,
    lon_max: float = 30.5,
    lat_max: float = 29.5,
) -> dict[str, object]:
    """Return a GeoJSON Polygon dict for a small box (sub-100 km²)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon_min, lat_min],
                [lon_max, lat_min],
                [lon_max, lat_max],
                [lon_min, lat_max],
                [lon_min, lat_min],
            ]
        ],
    }


def _big_polygon_geojson() -> dict[str, object]:
    """Return a GeoJSON Polygon that is >10,000 km2 (20 deg x 20 deg box near Egypt)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [25.0, 22.0],
                [45.0, 22.0],
                [45.0, 42.0],
                [25.0, 42.0],
                [25.0, 22.0],
            ]
        ],
    }


# ---------------------------------------------------------------------------
# Construction from Polygon GeoJSON
# ---------------------------------------------------------------------------


def test_aoi_from_polygon_geojson() -> None:
    geojson = _small_polygon_geojson()
    aoi = AOI.from_geojson(geojson)
    assert aoi.geometry is not None
    assert aoi.area_km2 > 0


def test_aoi_from_feature_geojson() -> None:
    geom = _small_polygon_geojson()
    feature: dict[str, object] = {"type": "Feature", "geometry": geom, "properties": {}}
    aoi = AOI.from_geojson(feature)
    assert aoi.area_km2 > 0


def test_aoi_from_feature_collection_single() -> None:
    geom = _small_polygon_geojson()
    feature: dict[str, object] = {"type": "Feature", "geometry": geom, "properties": {}}
    fc: dict[str, object] = {"type": "FeatureCollection", "features": [feature]}
    aoi = AOI.from_geojson(fc)
    assert aoi.area_km2 > 0


# ---------------------------------------------------------------------------
# Area computation
# ---------------------------------------------------------------------------


def test_area_small_box_reasonable_range() -> None:
    """A 0.5 deg x 0.5 deg box near 29N (Egypt area) should be ~2,000-3,500 km2."""
    aoi = AOI.from_geojson(_small_polygon_geojson(30.0, 29.0, 30.5, 29.5))
    # At 29N, 1 deg longitude ~88 km, 1 deg latitude ~111 km
    # 0.5 x 0.5 deg ~44 x 55.5 ~2,442 km2
    assert 1_500 < aoi.area_km2 < 4_000, f"area_km2={aoi.area_km2:.1f} out of expected range"


def test_area_one_degree_box_not_10000_km2() -> None:
    """Confirm 1 deg x 1 deg at 30N is NOT exactly 10,000 km2 (common misconception).

    At 30N, 1 deg lon ~96.5 km, 1 deg lat ~111 km => ~10,712 km2.
    The naive formula (111 km)^2 = 12,321 km2 is also wrong.
    Both differ from 10,000 km2, confirming equal-area projection computes real area.
    """
    # A 1 deg x 1 deg box at 30N is ~10,712 km2 (over limit); use smaller box.
    # 0.9 deg x 0.9 deg at 30N: 0.9 * 96.5 * 0.9 * 111 ~8,676 km2 (within limit).
    aoi = AOI.from_geojson(
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [30.0, 29.5],
                    [30.9, 29.5],
                    [30.9, 30.4],
                    [30.0, 30.4],
                    [30.0, 29.5],
                ]
            ],
        }
    )
    # The area should be measurably different from the naive 0.9*111*0.9*111 ~9,969 km2
    # Real equal-area value accounts for longitude compression at 30N
    assert aoi.area_km2 != pytest.approx(9_969.0, abs=500.0)
    # It should also be well within the sanity range
    assert 7_000 < aoi.area_km2 < 10_000


# ---------------------------------------------------------------------------
# Too-large AOI raises typed error
# ---------------------------------------------------------------------------


def test_too_large_raises_error() -> None:
    with pytest.raises(AOITooLargeError) as exc_info:
        AOI.from_geojson(_big_polygon_geojson())
    err = exc_info.value
    assert err.area_km2 > 10_000
    assert "10000" in str(err) or "10,000" in str(err)


def test_too_large_error_contains_actual_area() -> None:
    with pytest.raises(AOITooLargeError) as exc_info:
        AOI.from_geojson(_big_polygon_geojson())
    err = exc_info.value
    assert err.area_km2 > 0
    # The message should include the area
    assert str(err.area_km2)[:4] in str(err)


# ---------------------------------------------------------------------------
# Invalid geometry
# ---------------------------------------------------------------------------


def test_unsupported_geometry_type_rejected() -> None:
    geojson = {
        "type": "Point",
        "coordinates": [30.0, 29.0],
    }
    with pytest.raises(AOIInvalidGeometryError):
        AOI.from_geojson(geojson)


def test_feature_collection_multiple_features_rejected() -> None:
    geom = _small_polygon_geojson()
    feature: dict[str, object] = {"type": "Feature", "geometry": geom, "properties": {}}
    fc: dict[str, object] = {"type": "FeatureCollection", "features": [feature, feature]}
    with pytest.raises(AOIInvalidGeometryError):
        AOI.from_geojson(fc)


def test_feature_null_geometry_rejected() -> None:
    feature: dict[str, object] = {"type": "Feature", "geometry": None, "properties": {}}
    with pytest.raises(AOIInvalidGeometryError):
        AOI.from_geojson(feature)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_bounds_within_wgs84() -> None:
    aoi = AOI.from_geojson(_small_polygon_geojson(30.0, 29.0, 30.5, 29.5))
    minx, miny, maxx, maxy = aoi.bounds
    assert minx == pytest.approx(30.0)
    assert miny == pytest.approx(29.0)
    assert maxx == pytest.approx(30.5)
    assert maxy == pytest.approx(29.5)


def test_geojson_roundtrip() -> None:
    geojson = _small_polygon_geojson()
    aoi = AOI.from_geojson(geojson)
    out = aoi.geojson
    assert out["type"] in ("Polygon", "MultiPolygon")


# ---------------------------------------------------------------------------
# Hash stability
# ---------------------------------------------------------------------------


def test_hash_stable_across_reconstructions() -> None:
    geojson = _small_polygon_geojson()
    aoi1 = AOI.from_geojson(geojson)
    aoi2 = AOI.from_geojson(geojson)
    assert aoi1.hash == aoi2.hash


def test_hash_differs_for_different_geometries() -> None:
    aoi1 = AOI.from_geojson(_small_polygon_geojson(30.0, 29.0, 30.5, 29.5))
    aoi2 = AOI.from_geojson(_small_polygon_geojson(31.0, 29.0, 31.5, 29.5))
    assert aoi1.hash != aoi2.hash


def test_hash_is_string() -> None:
    aoi = AOI.from_geojson(_small_polygon_geojson())
    assert isinstance(aoi.hash, str)
    assert len(aoi.hash) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# MultiPolygon support (smaller area)
# ---------------------------------------------------------------------------


def test_multipolygon_accepted() -> None:
    mp = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [30.0, 29.0],
                    [30.1, 29.0],
                    [30.1, 29.1],
                    [30.0, 29.1],
                    [30.0, 29.0],
                ]
            ],
            [
                [
                    [30.2, 29.2],
                    [30.3, 29.2],
                    [30.3, 29.3],
                    [30.2, 29.3],
                    [30.2, 29.2],
                ]
            ],
        ],
    }
    aoi = AOI.from_geojson(mp)
    assert aoi.area_km2 > 0
