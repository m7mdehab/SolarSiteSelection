"""Weighted overlay engine: exclusion masks, LSI computation, and class assignment.

Overview
--------
The overlay pipeline has four stages:

1. ``build_exclusion_mask`` — combine one or more named binary masks / distance
   rasters into a single boolean exclusion mask according to registry rules.
   (The ``acquire`` module's landcover exclusion mask can be passed in directly
   as a pre-computed layer; this function does NOT import from acquire.)

2. ``weighted_overlay`` — compute the Land Suitability Index (LSI) as a weighted
   sum over suitability layers:

       LSI = Σ  w_i * s_i   over criteria present in ``suitability_layers``

   where w_i is renormalised to sum to 1 over the *present* criteria if some
   expected layers are missing (see ``weighted_overlay`` docstring).

3. ``apply_exclusions`` — set excluded cells to NaN (default) or 0.

4. ``classify_lsi`` — quantile or equal-interval reclassification of the
   continuous LSI into the five LSI classes (5 = most suitable … 1 = restricted).

All operations are pure xarray; no rasterio or GDAL dependencies.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import xarray as xr

from solarsite.analysis.registry import CriteriaRegistry

__all__ = [
    "apply_exclusions",
    "build_exclusion_mask",
    "classify_lsi",
    "weighted_overlay",
]


# ---------------------------------------------------------------------------
# 1. Exclusion mask
# ---------------------------------------------------------------------------


def build_exclusion_mask(
    named_masks: dict[str, xr.DataArray],
    registry: CriteriaRegistry,
) -> xr.DataArray:
    """Combine named binary/distance masks into a single boolean exclusion mask.

    This function is intentionally generic: it accepts a dict of pre-computed
    masks keyed by their ``HardExclusionRule.key`` and combines them with a
    logical OR.  The landcover module already provides an ``exclusion_mask``
    for WorldCover + WDPA; pass it in directly under the appropriate key.

    Parameters
    ----------
    named_masks:
        Dict mapping exclusion rule key (e.g. ``"excl_wdpa"``) to a DataArray
        that is truthy where cells must be excluded.  Boolean, uint8, or float
        arrays are accepted; non-zero → excluded.  All arrays must be on the
        same grid (same shape, dims, and coordinates).
    registry:
        The loaded ``CriteriaRegistry``.  Used to validate that supplied keys
        correspond to known hard-exclusion rules.  Unknown keys produce a
        ``UserWarning`` but are still incorporated (defensive approach).

    Returns
    -------
    xr.DataArray
        Boolean DataArray (dtype bool) where ``True`` means *excluded*.
        Shape and coordinates match the input masks.  If ``named_masks`` is
        empty, returns a grid of ``False`` (nothing excluded) matching the
        first-available layer shape (raises ``ValueError`` if no layers given).

    Notes
    -----
    Hard-exclusion rules in the registry describe *when* to exclude; the actual
    distance/class computations are performed upstream.  This function trusts the
    caller to pass in correctly pre-computed masks.
    """
    import warnings

    if not named_masks:
        raise ValueError("named_masks must contain at least one entry.")

    known_keys = {r.key for r in registry.hard_exclusions}

    # Validate keys (warn on unknown, don't fail)
    for key in named_masks:
        if key not in known_keys:
            warnings.warn(
                f"build_exclusion_mask: key '{key}' is not in the registry's "
                "hard_exclusion_rules; incorporating it anyway.",
                UserWarning,
                stacklevel=2,
            )

    # Combine with logical OR
    masks_iter = iter(named_masks.values())
    first = next(masks_iter)
    combined = (first != 0).astype(bool)

    for mask in masks_iter:
        combined = combined | (mask != 0)

    result = combined.copy()
    result.name = "exclusion_mask"
    result.attrs.update(
        {
            "long_name": "Hard exclusion mask (True = excluded)",
            "keys_included": list(named_masks.keys()),
        }
    )
    return result


# ---------------------------------------------------------------------------
# 2. Weighted overlay
# ---------------------------------------------------------------------------


def weighted_overlay(
    suitability_layers: dict[str, xr.DataArray],
    registry: CriteriaRegistry,
) -> xr.DataArray:
    """Compute the Land Suitability Index (LSI) via weighted sum.

    LSI = Σ  w_i * s_i   over the factor criteria present in
    ``suitability_layers``, with weights renormalised over present criteria.

    Parameters
    ----------
    suitability_layers:
        Dict mapping criterion key → suitability DataArray (values in [0, 1]).
        Keys should match ``registry.factors`` criterion keys.  Extra keys are
        ignored with a ``UserWarning``; missing keys cause their weight to be
        redistributed to the present criteria (see Notes).
    registry:
        Loaded ``CriteriaRegistry`` supplying global weights.

    Returns
    -------
    xr.DataArray
        Continuous LSI in [0, 1].  Shape and coordinates match the input layers.
        NaN where ALL input suitability layers are NaN.

    Notes
    -----
    **Missing-layer weight renormalisation**: if only a subset of factor
    criteria are present, the global weights of present criteria are
    renormalised so they still sum to 1.0.  This preserves the relative
    ordering of present criteria while producing a valid [0, 1] LSI.
    For example, if only two criteria with raw global weights 0.3 and 0.2 are
    present, their renormalised weights become 0.6 and 0.4 respectively.

    If ``suitability_layers`` is empty (no criteria provided), returns a
    zero-filled DataArray using the shape of the first registry factor's key.
    """
    import warnings

    all_weights = registry.all_global_weights()  # {key: weight}
    factor_keys = set(all_weights.keys())

    # Warn on extra keys
    for key in suitability_layers:
        if key not in factor_keys:
            warnings.warn(
                f"weighted_overlay: key '{key}' is not a factor criterion in the "
                "registry; ignoring.",
                UserWarning,
                stacklevel=2,
            )

    # Determine present factor keys
    present_keys = [k for k in suitability_layers if k in factor_keys]

    if not present_keys:
        raise ValueError(
            "weighted_overlay: no valid factor criteria found in suitability_layers. "
            f"Expected one or more of: {sorted(factor_keys)}"
        )

    # Renormalise weights over present criteria
    raw_weights = {k: all_weights[k] for k in present_keys}
    total_raw = sum(raw_weights.values())
    if total_raw <= 0.0:
        raise ValueError("Sum of weights for present criteria is zero or negative.")
    renorm_weights = {k: w / total_raw for k, w in raw_weights.items()}

    # Build weighted sum (pure xarray)
    # Use the first layer as the template
    first_layer = suitability_layers[present_keys[0]]
    lsi = xr.zeros_like(first_layer, dtype=np.float64)
    lsi.values[:] = 0.0

    # Track where at least one non-NaN value exists
    valid_count = xr.zeros_like(first_layer, dtype=np.float64)
    valid_count.values[:] = 0.0

    for key in present_keys:
        layer = suitability_layers[key]
        w = renorm_weights[key]
        # Replace NaN with 0 for summation; track weight of non-NaN cells
        not_nan = (~np.isnan(layer.values)).astype(np.float64)
        filled = np.where(np.isnan(layer.values), 0.0, layer.values)
        lsi.values += w * filled
        valid_count.values += w * not_nan

    # Renormalise by the actual weight sum that contributed (handles per-cell NaNs)
    # Avoid division by zero
    with np.errstate(invalid="ignore", divide="ignore"):
        lsi.values = np.where(
            valid_count.values > 0.0,
            lsi.values / valid_count.values,
            np.nan,
        )

    lsi.name = "lsi"
    lsi.attrs.update(
        {
            "long_name": "Land Suitability Index",
            "units": "suitability_score_0_1",
            "criteria_used": present_keys,
            "weight_renormalised": total_raw < 1.0 - 1e-9,
        }
    )
    return lsi


# ---------------------------------------------------------------------------
# 3. Apply exclusions
# ---------------------------------------------------------------------------


def apply_exclusions(
    lsi: xr.DataArray,
    mask: xr.DataArray,
    *,
    fill_value: float = float("nan"),
) -> xr.DataArray:
    """Set excluded cells to ``fill_value`` (default NaN).

    Parameters
    ----------
    lsi:
        Continuous LSI DataArray.
    mask:
        Boolean or numeric DataArray where truthy values indicate exclusion.
        Must be broadcastable to ``lsi``.
    fill_value:
        Value assigned to excluded cells.  Default ``NaN`` (excluded cells
        are treated as missing/unsuitable in downstream analysis).
        Pass ``0.0`` to use an explicit zero instead.

    Returns
    -------
    xr.DataArray
        LSI with excluded cells set to ``fill_value``.

    Notes
    -----
    Excluded cells receive ``fill_value`` regardless of their original LSI value.
    Non-excluded cells are unchanged (including existing NaNs).
    """
    excluded = (mask != 0).values
    result_values = lsi.values.copy()
    result_values[excluded] = fill_value

    result = xr.DataArray(
        result_values,
        dims=lsi.dims,
        coords=lsi.coords,
        name=lsi.name,
        attrs={**lsi.attrs, "exclusions_applied": True, "exclusion_fill": fill_value},
    )
    try:
        crs = lsi.rio.crs
        if crs is not None:
            result = result.rio.write_crs(crs, inplace=True)
    except AttributeError:
        pass
    return result


# ---------------------------------------------------------------------------
# 4. Classify LSI
# ---------------------------------------------------------------------------

LsiMethod = Literal["quantile", "equal_interval"]


def classify_lsi(
    lsi: xr.DataArray,
    n_classes: int = 5,
    method: LsiMethod = "quantile",
) -> xr.DataArray:
    """Reclassify a continuous LSI into discrete land suitability classes.

    Parameters
    ----------
    lsi:
        Continuous LSI DataArray (values in [0, 1]).
    n_classes:
        Number of classes (default 5, matching the five LSI classes in
        criteria.yaml: 5 = most suitable, 1 = least suitable / restricted).
    method:
        ``"quantile"`` (default) — class boundaries are set at equal-quantile
        intervals of the **non-NaN** LSI values.  Each class contains roughly
        the same number of valid pixels.

        ``"equal_interval"`` — class boundaries are set at equal steps between
        the minimum and maximum non-NaN LSI values.

    Returns
    -------
    xr.DataArray
        Integer class raster (dtype int32).  Values in ``[1, n_classes]``
        where ``n_classes`` = most suitable and ``1`` = least suitable.
        NaN pixels in the input are preserved as -9999 (nodata integer).

    Notes
    -----
    Class assignment: the continuous LSI range is divided into ``n_classes``
    equal bins (by quantile or by value range).  Bin 1 = lowest values =
    least suitable; bin n_classes = highest values = most suitable.

    For ``quantile`` method, ties at quantile boundaries are broken
    conservatively (lower class wins for exact boundary values, except for the
    topmost class).

    For ``equal_interval`` method, thresholds are computed as:
        t_i = lsi_min + i/n_classes * (lsi_max - lsi_min)  for i = 1..n_classes-1

    NODATA encoding: excluded/NaN cells are stored as -9999 in the output
    integer array.  Downstream consumers should treat -9999 as NoData.
    """
    NODATA = -9999

    flat = lsi.values.ravel()
    valid_mask = ~np.isnan(flat)
    valid_vals = flat[valid_mask]

    out = np.full(lsi.values.shape, NODATA, dtype=np.int32)

    if valid_vals.size == 0:
        # All NaN — return all NODATA
        return xr.DataArray(
            out,
            dims=lsi.dims,
            coords=lsi.coords,
            name="lsi_class",
            attrs={
                "long_name": "LSI suitability class (1=least … n=most)",
                "nodata": NODATA,
                "method": method,
                "n_classes": n_classes,
            },
        )

    if method == "quantile":
        # Quantile-based thresholds: n_classes - 1 internal boundaries
        quantiles = np.linspace(0.0, 1.0, n_classes + 1)  # includes 0 and 1
        thresholds = np.quantile(valid_vals, quantiles)
        # thresholds[0] = min, thresholds[-1] = max
    elif method == "equal_interval":
        vmin = float(valid_vals.min())
        vmax = float(valid_vals.max())
        thresholds = np.linspace(vmin, vmax, n_classes + 1)
    else:
        raise ValueError(f"Unknown method '{method}'. Expected 'quantile' or 'equal_interval'.")

    # Assign classes: class i (1-based) covers (thresholds[i-1], thresholds[i]]
    # The lowest class also includes values == thresholds[0] (the minimum).
    flat_vals = lsi.values.ravel().copy()
    flat_out = out.ravel()

    for idx in range(len(flat_vals)):
        v = flat_vals[idx]
        if np.isnan(v):
            flat_out[idx] = NODATA
            continue
        # Find which bin: bin 1 = [t0, t1], bin 2 = (t1, t2], ...
        # np.searchsorted gives the insertion point into thresholds[1:-1] (the
        # n_classes-1 internal boundaries).
        boundaries = thresholds[1:-1]  # n_classes - 1 values
        cls = int(np.searchsorted(boundaries, v, side="left")) + 1
        # Clamp to [1, n_classes]
        cls = max(1, min(n_classes, cls))
        flat_out[idx] = cls

    out = flat_out.reshape(lsi.values.shape)

    result = xr.DataArray(
        out,
        dims=lsi.dims,
        coords=lsi.coords,
        name="lsi_class",
        attrs={
            "long_name": "LSI suitability class (1=least … n=most)",
            "nodata": NODATA,
            "method": method,
            "n_classes": n_classes,
            "thresholds": thresholds.tolist(),
        },
    )
    try:
        crs = lsi.rio.crs
        if crs is not None:
            result = result.rio.write_crs(crs, inplace=True)
    except AttributeError:
        pass
    return result
