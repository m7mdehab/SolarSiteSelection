"""Analysis engine: AHP, reclassification, weighted overlay, site extraction, energy."""

from solarsite.analysis.ahp import (
    AHPError,
    AHPResult,
    InconsistencyError,
    ahp_weights,
    ahp_weights_strict,
)
from solarsite.analysis.energy import (
    EnergyAssumptions,
    EnergyResult,
    site_energy,
    site_energy_from_ghi,
    specific_yield,
)
from solarsite.analysis.losses import LossLineItem, LossStack
from solarsite.analysis.overlay import (
    apply_exclusions,
    build_exclusion_mask,
    classify_lsi,
    weighted_overlay,
)
from solarsite.analysis.reclassify import reclassify_layer
from solarsite.analysis.registry import CriteriaRegistry, load_registry
from solarsite.analysis.sites import SITE_COLUMNS, extract_sites

__all__ = [
    "SITE_COLUMNS",
    "AHPError",
    "AHPResult",
    "CriteriaRegistry",
    "EnergyAssumptions",
    "EnergyResult",
    "InconsistencyError",
    "LossLineItem",
    "LossStack",
    "ahp_weights",
    "ahp_weights_strict",
    "apply_exclusions",
    "build_exclusion_mask",
    "classify_lsi",
    "extract_sites",
    "load_registry",
    "reclassify_layer",
    "site_energy",
    "site_energy_from_ghi",
    "specific_yield",
    "weighted_overlay",
]
