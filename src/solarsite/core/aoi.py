"""AOI (Area of Interest) model with validation and area limiting.

Constructs an AOI from a GeoJSON dict (Polygon, Feature, or FeatureCollection),
validates geometry, computes area using an equal-area projection, and enforces
a maximum area of 10,000 km².
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, field_validator, model_validator
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

# Equal-area projection for area computations (EPSG:6933 — WGS 84 / NSIDC EASE-Grid 2.0)
# This is a global equal-area cylindrical projection suitable for area calculations
# anywhere on Earth without having to pick a local UTM zone.
_AREA_CRS_EPSG = 6933
_MAX_AREA_KM2 = 10_000.0


class AOITooLargeError(ValueError):
    """Raised when the AOI area exceeds the configured maximum."""

    def __init__(self, area_km2: float, max_km2: float = _MAX_AREA_KM2) -> None:
        self.area_km2 = area_km2
        self.max_km2 = max_km2
        super().__init__(
            f"AOI area {area_km2:.2f} km² exceeds the maximum allowed {max_km2:.0f} km²."
        )


class AOIInvalidGeometryError(ValueError):
    """Raised when the input geometry is invalid or unsupported."""


def _extract_geometry(geojson: dict[str, Any]) -> BaseGeometry:
    """Extract a shapely geometry from a GeoJSON dict."""
    gtype = geojson.get("type")
    if gtype == "FeatureCollection":
        features = geojson.get("features", [])
        if len(features) != 1:
            raise AOIInvalidGeometryError(
                f"FeatureCollection must contain exactly 1 feature, got {len(features)}."
            )
        return _extract_geometry(features[0])
    elif gtype == "Feature":
        geom = geojson.get("geometry")
        if geom is None:
            raise AOIInvalidGeometryError("Feature has a null geometry.")
        return _extract_geometry(geom)
    elif gtype in ("Polygon", "MultiPolygon"):
        try:
            return shape(geojson)
        except Exception as exc:
            raise AOIInvalidGeometryError(f"Could not parse geometry: {exc}") from exc
    else:
        raise AOIInvalidGeometryError(
            f"Unsupported geometry type '{gtype}'. Expected Polygon or MultiPolygon."
        )


def _compute_area_km2(geom: BaseGeometry) -> float:
    """Compute area in km² using EPSG:6933 equal-area projection."""
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{_AREA_CRS_EPSG}", always_xy=True)
    projected = _transform_geometry(geom, transformer)
    return projected.area / 1_000_000.0  # m² → km²


def _transform_geometry(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    """Transform a shapely geometry using a pyproj Transformer."""
    from shapely.ops import transform

    return transform(transformer.transform, geom)


def _stable_hash(geom: BaseGeometry) -> str:
    """Compute a deterministic SHA-256 hash of a geometry's GeoJSON representation."""
    # Use a canonical (sorted-key) JSON of the geometry mapping for stability
    geojson_str = json.dumps(mapping(geom), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(geojson_str.encode()).hexdigest()


class AOI(BaseModel):
    """Validated Area of Interest with area limits and equal-area computation."""

    model_config = {"arbitrary_types_allowed": True}

    # Parsed shapely geometry (WGS-84 / EPSG:4326 coordinates)
    geometry: BaseGeometry

    # Computed properties
    area_km2: float
    bounds: tuple[float, float, float, float]  # (minx, miny, maxx, maxy) in WGS-84
    hash: str

    @classmethod
    def from_geojson(cls, geojson: dict[str, Any]) -> AOI:
        """Construct an AOI from a GeoJSON dict (Polygon, Feature, or FeatureCollection)."""
        geom = _extract_geometry(geojson)

        if not geom.is_valid:
            raise AOIInvalidGeometryError(
                f"Geometry is not valid: {geom.geom_type}. "
                "Check for self-intersections or other topology errors."
            )

        area_km2 = _compute_area_km2(geom)

        if area_km2 > _MAX_AREA_KM2:
            raise AOITooLargeError(area_km2)

        bounds_raw = geom.bounds  # (minx, miny, maxx, maxy)
        bounds: tuple[float, float, float, float] = (
            bounds_raw[0],
            bounds_raw[1],
            bounds_raw[2],
            bounds_raw[3],
        )

        return cls(
            geometry=geom,
            area_km2=area_km2,
            bounds=bounds,
            hash=_stable_hash(geom),
        )

    @property
    def geojson(self) -> dict[str, Any]:
        """Return the geometry as a GeoJSON Polygon/MultiPolygon dict."""
        return dict(mapping(self.geometry))

    @field_validator("geometry", mode="before")
    @classmethod
    def _validate_geometry(cls, v: Any) -> Any:
        if not isinstance(v, BaseGeometry):
            raise ValueError(f"Expected a shapely geometry, got {type(v)}")
        return v

    @model_validator(mode="after")
    def _check_area(self) -> AOI:
        # Area was already validated in from_geojson, but we double-check
        if self.area_km2 > _MAX_AREA_KM2:
            raise AOITooLargeError(self.area_km2)
        return self
