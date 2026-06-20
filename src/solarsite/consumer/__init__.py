"""Consumer rooftop-PV mode (Track B).

A separate analysis mode for a household's own roof. Capacity derives from roof
area x module efficiency (never a land packing density), so it cannot produce a
utility-scale "carpet the region" figure. Energy outputs are real; monetary
outputs are ``None`` until their region-specific inputs are verified.

``RECOMMENDED_RANGES`` documents published RANGES (not point values) for each
``NEEDS_HUMAN_DECISION`` economic input, for the morning queue and the UI. These
are ranges to inform a human decision — the engine does NOT use them as values.
"""

from __future__ import annotations

from solarsite.consumer.rooftop import (
    analyze_rooftop,
    annual_production_kwh,
    compute_economics,
    energy_balance,
    roof_capacity_kwp,
)
from solarsite.consumer.schemas import (
    ConsumerResult,
    ConsumptionInput,
    EconomicInputs,
    EconomicsResult,
    EnergyBalance,
    RoofInput,
    RooftopAnalysisRequest,
)

__all__ = [
    "RECOMMENDED_RANGES",
    "ConsumerResult",
    "ConsumptionInput",
    "EconomicInputs",
    "EconomicsResult",
    "EnergyBalance",
    "RoofInput",
    "RooftopAnalysisRequest",
    "analyze_rooftop",
    "annual_production_kwh",
    "compute_economics",
    "energy_balance",
    "roof_capacity_kwp",
]

#: Published RANGES for the NEEDS_HUMAN_DECISION economic inputs. Ranges, not
#: values — to inform a human, never consumed by the engine. Each range cites a
#: real, verified source inline.
RECOMMENDED_RANGES: dict[str, dict[str, str]] = {
    "install_cost_usd_per_w": {
        "range": "3.2-5.5 USD/W (US residential, 20th-80th pct)",
        "point_us_2024": "3.25 USD/W (NREL benchmark)",
        "source": "LBNL Tracking the Sun 2024; NREL/TP-7A40-92536 (Ramasamy et al. 2025)",
        "caveat": "US figures; outside the US the value differs and is unverified here.",
    },
    "retail_tariff_usd_per_kwh": {
        "range": "highly region-specific (rough global span ~0.05-0.45 USD/kWh)",
        "point_us_2024": "NOT committed — varies by utility/state/country",
        "source": "no single verified source applies to an arbitrary location",
        "caveat": "Must come from the user's actual electricity bill / local utility.",
    },
    "export_rate_usd_per_kwh": {
        "range": "policy-dependent: full net metering (= retail) down to ~0",
        "point_us_2024": "NOT committed — net-metering rules vary by jurisdiction",
        "source": "policy, not a measurable constant",
        "caveat": "Depends on the local net-metering / feed-in regime.",
    },
    "incentive_usd": {
        "range": "0 to large (e.g. US federal ITC ~30% of cost in 2024)",
        "point_us_2024": "NOT committed — program- and year-specific",
        "source": "program-specific",
        "caveat": "Depends on national/local incentive programs in force.",
    },
    "om_cost_usd_per_kw_yr": {
        "range": "~15-35 USD/kW/yr (residential)",
        "point_us_2024": "NREL benchmark ~35 USD/kWdc/yr (2024)",
        "source": "NREL/TP-7A40-92536 Table A-6",
        "caveat": "Order-of-magnitude; verify for the install.",
    },
}
