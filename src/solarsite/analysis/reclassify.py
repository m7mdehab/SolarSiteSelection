"""Reclassification engine: convert raw criterion layers → 0..1 suitability rasters.

Band semantics (BreakpointReclassification)
-------------------------------------------
Breakpoints are ordered upper-bound-inclusive bands:

    band 0: value <= breakpoints[0].max  →  breakpoints[0].score
    band i: breakpoints[i-1].max < value <= breakpoints[i].max  →  breakpoints[i].score

The last breakpoint typically has max=inf to catch all higher values.
NaN pixels in the input are preserved as NaN in the output.

Unit reconciliation (DECISION #9 — GHI annual→daily)
-----------------------------------------------------
The ``solar_radiation`` criterion declares ``unit = "kWh/m2/day"`` but PVGIS
delivers annual totals in kWh/m²/yr (~2200).  When the criterion unit ends with
"/day" and the *data unit* is annual (ends with "/yr" or "/year"), reclassify_layer
divides the data by 365 before applying breakpoints.

The conversion is explicit and logged in the docstring.  The caller can supply an
explicit ``data_unit`` string to override auto-detection.  If no unit metadata is
attached to the DataArray and ``data_unit`` is also None, reclassify_layer applies
**no conversion** and emits a warning so the caller knows.

ClassScoreReclassification
--------------------------
Integer or string class codes are looked up in the class_scores dict.
Unknown codes are mapped to score 0.0 (conservative — unknown = unsuitable).
This is documented here and in the code.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

import numpy as np
import xarray as xr

if TYPE_CHECKING:
    pass

from solarsite.analysis.registry import (
    BreakpointReclassification,
    ClassScoreReclassification,
    Criterion,
)

__all__ = ["reclassify_layer"]


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

_ANNUAL_SUFFIXES = ("/yr", "/year", "yr", "year")
_DAILY_SUFFIXES = ("/day", "/d")


def _is_annual(unit: str) -> bool:
    """Return True if the unit string looks like an annual quantity."""
    u = unit.lower().strip()
    return any(u.endswith(s) for s in _ANNUAL_SUFFIXES)


def _is_daily(unit: str) -> bool:
    """Return True if the unit string looks like a per-day quantity."""
    u = unit.lower().strip()
    return any(u.endswith(s) for s in _DAILY_SUFFIXES)


def _resolve_data_unit(da: xr.DataArray, data_unit: str | None) -> str:
    """Resolve the effective data unit.

    Priority: explicit ``data_unit`` arg > DataArray ``attrs['units']`` > empty string.
    """
    if data_unit is not None:
        return data_unit
    # rioxarray / CF convention store unit in attrs["units"] or attrs["unit"]
    attrs = da.attrs
    return str(attrs.get("units", attrs.get("unit", "")))


def _maybe_convert_to_daily(
    values: np.ndarray,
    criterion_unit: str,
    data_unit: str,
    criterion_key: str,
) -> np.ndarray:
    """Apply annual→daily conversion when units require it.

    Rules
    -----
    * criterion says /day AND data is annual → divide by 365.
    * criterion says /day AND data is already daily → no-op.
    * criterion says /day AND data unit is unknown → warn, no-op.
    * criterion does NOT say /day → no-op regardless of data unit.

    Returns a (possibly new) numpy array.
    """
    if not _is_daily(criterion_unit):
        return values

    if _is_annual(data_unit):
        # Explicit annual→daily normalisation: GHI yr→day ÷ 365
        return values / 365.0

    if _is_daily(data_unit) or data_unit == "":
        if data_unit == "":
            warnings.warn(
                f"Criterion '{criterion_key}' expects units in /day but the layer "
                "carries no unit metadata and no data_unit was supplied.  "
                "Assuming data is already in /day — pass data_unit='kWh/m2/yr' "
                "if the layer is an annual total.",
                UserWarning,
                stacklevel=4,
            )
        return values

    # Unrecognised data unit but criterion is /day — warn and pass through
    warnings.warn(
        f"Criterion '{criterion_key}' expects units in /day but data unit "
        f"'{data_unit}' is unrecognised.  No conversion applied.",
        UserWarning,
        stacklevel=4,
    )
    return values


# ---------------------------------------------------------------------------
# Breakpoint reclassification
# ---------------------------------------------------------------------------


def _apply_breakpoints(
    values: np.ndarray,
    spec: BreakpointReclassification,
) -> np.ndarray:
    """Vectorised breakpoint reclassification.

    Band semantics (upper-bound-inclusive):
        band 0 : value <= bp[0].max          → bp[0].score
        band i : bp[i-1].max < value <= bp[i].max  → bp[i].score

    NaN pixels produce NaN in the output.
    """
    bps = spec.breakpoints
    out = np.full(values.shape, np.nan, dtype=np.float64)

    # Iterate from lowest band upward so the first matching band wins.
    # We set pixels that have NOT yet been assigned (still NaN) and that fall
    # within the current band.
    unassigned = ~np.isnan(values)  # start: everything non-NaN is unassigned
    assigned = np.zeros(values.shape, dtype=bool)

    for bp in bps:
        in_band = unassigned & ~assigned & (values <= bp.max)
        out[in_band] = bp.score
        assigned |= in_band

    # Remaining unassigned (non-NaN) pixels fall above all finite maxes — this
    # should only happen if the last breakpoint is not inf; assign score 0.
    remaining = unassigned & ~assigned
    if remaining.any():
        out[remaining] = 0.0

    return out


# ---------------------------------------------------------------------------
# Class-score reclassification
# ---------------------------------------------------------------------------


def _apply_class_scores(
    values: np.ndarray,
    spec: ClassScoreReclassification,
) -> np.ndarray:
    """Vectorised class-score reclassification.

    The class_scores dict may have string or integer keys; both are matched
    by converting the value to a string.

    Unknown class codes → score 0.0  (conservative / unsuitable default).
    NaN pixels → NaN in the output.
    """
    out = np.full(values.shape, np.nan, dtype=np.float64)
    flat_values = values.ravel()
    flat_out = out.ravel()

    for i, v in enumerate(flat_values):
        if np.isnan(float(v)) if np.issubdtype(type(v), np.floating) else False:
            flat_out[i] = np.nan
            continue
        # Try exact string match first
        key = str(int(v)) if isinstance(v, (np.integer, int, float)) else str(v)
        if key in spec.class_scores:
            flat_out[i] = spec.class_scores[key]
        else:
            # Try original string form (handles string classes like "VII")
            key2 = str(v)
            if key2 in spec.class_scores:
                flat_out[i] = spec.class_scores[key2]
            else:
                flat_out[i] = 0.0  # unknown class → 0 (unsuitable)

    return flat_out.reshape(values.shape)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reclassify_layer(
    da: xr.DataArray,
    criterion: Criterion,
    *,
    data_unit: str | None = None,
) -> xr.DataArray:
    """Apply criterion reclassification rules to a raw data layer.

    Parameters
    ----------
    da:
        Raw input DataArray aligned to the working grid (y, x) dims.
        Values must be in the units described by ``data_unit`` or the
        DataArray's own ``attrs['units']``.
    criterion:
        The ``Criterion`` from the registry that owns the reclassification spec.
    data_unit:
        Explicit unit of the data, e.g. ``"kWh/m2/yr"``.  Overrides any unit
        metadata stored in ``da.attrs``.  Pass this when the layer has no unit
        metadata (e.g. raw PVGIS annual-total raster).

    Returns
    -------
    xr.DataArray
        Suitability raster in the range [0, 1].  NaN where input was NaN.
        Carries ``attrs['long_name']`` and ``attrs['criterion_key']`` for
        provenance.

    Notes
    -----
    **Unit reconciliation** (DECISION #9):
    The ``solar_radiation`` criterion breakpoints are in kWh/m²/**day** but the
    PVGIS layer is in kWh/m²/**yr**.  When ``criterion.unit`` ends with "/day"
    and the resolved data unit is annual (ends with "/yr" or "/year"), the data
    is divided by 365 before applying breakpoints.  No silent mis-scaling occurs:
    a ``UserWarning`` is emitted if the data unit is unknown.

    **ClassScoreReclassification**:
    Unknown class codes are mapped to 0.0 (conservative / unsuitable).
    """
    values = da.values.copy().astype(np.float64)

    # --- Unit reconciliation -----------------------------------------------
    effective_data_unit = _resolve_data_unit(da, data_unit)
    values = _maybe_convert_to_daily(
        values,
        criterion_unit=criterion.unit,
        data_unit=effective_data_unit,
        criterion_key=criterion.key,
    )

    # --- Reclassification ---------------------------------------------------
    reclass = criterion.reclassification
    if isinstance(reclass, BreakpointReclassification):
        out_values = _apply_breakpoints(values, reclass)
    elif isinstance(reclass, ClassScoreReclassification):
        out_values = _apply_class_scores(values, reclass)
    else:
        raise TypeError(f"Unsupported reclassification type: {type(reclass)}")

    # --- Wrap in DataArray --------------------------------------------------
    result = xr.DataArray(
        out_values,
        dims=da.dims,
        coords=da.coords,
        name=f"suit_{criterion.key}",
        attrs={
            "long_name": f"Suitability: {criterion.name}",
            "criterion_key": criterion.key,
            "units": "suitability_score_0_1",
        },
    )

    # Preserve spatial metadata if present (guard: rioxarray accessor may not
    # be initialised on DataArrays without coordinate metadata).
    try:
        crs = da.rio.crs
        if crs is not None:
            result = result.rio.write_crs(crs, inplace=True)
    except AttributeError:
        pass

    return result
