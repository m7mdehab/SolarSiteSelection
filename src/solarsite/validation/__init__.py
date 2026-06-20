"""Validation backbone (Track F): physical-sanity bounds + cross-oracle checks.

This package exists so that no quantitative output can ship outside a defensible
physical/market envelope. A 61,557-GWh-class number, a 300 % capacity factor, a
$50/W rooftop install, or a negative payback are all caught here and surfaced
rather than displayed as fact.
"""

from solarsite.validation.sanity import (
    HOURS_PER_YEAR,
    MAX_MODULE_KWP_PER_M2,
    PHYSICAL_BOUNDS,
    Bound,
    SanityCheck,
    SanityViolation,
    assert_within,
    capacity_factor_from_specific_yield,
    check,
    check_energy_result,
    check_many,
    check_roof_capacity,
)

__all__ = [
    "HOURS_PER_YEAR",
    "MAX_MODULE_KWP_PER_M2",
    "PHYSICAL_BOUNDS",
    "Bound",
    "SanityCheck",
    "SanityViolation",
    "assert_within",
    "capacity_factor_from_specific_yield",
    "check",
    "check_energy_result",
    "check_many",
    "check_roof_capacity",
]
