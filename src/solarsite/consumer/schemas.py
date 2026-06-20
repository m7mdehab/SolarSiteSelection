"""Schemas for the consumer rooftop-PV mode (Track B).

This is a SEPARATE analysis mode from the utility-scale ground-mount engine. It
must never inherit the utility defaults (45 MWp/km², $1000/kWp): a household's
capacity comes from its actual roof area x module efficiency, not from a land
packing density, which is exactly why it cannot produce a "carpet-the-region"
61,557-GWh figure.

Honesty contract
----------------
* The ENERGY side (capacity, production, self-consumption split) is computed from
  physical inputs and is real.
* The MONEY side depends on values that are region- and policy-specific and that
  this project does NOT have verified for the user's area — install $/W, retail
  tariff, export/feed-in rate, incentives. Those fields default to ``None`` =
  ``NEEDS_HUMAN_DECISION``. When a required input is ``None`` the dependent
  output is ``None`` and a caveat is emitted; we never invent a value to fill it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "ConsumerResult",
    "ConsumptionInput",
    "EconomicInputs",
    "EconomicsResult",
    "EnergyBalance",
    "RoofInput",
    "RooftopAnalysisRequest",
]


class RoofInput(BaseModel):
    """The user's roof and module assumptions (all physical, all verifiable)."""

    area_m2: float = Field(..., gt=0.0, description="Gross roof-polygon area in m².")
    usable_fraction: float = Field(
        default=0.75,
        gt=0.0,
        le=1.0,
        description=(
            "Fraction of the gross roof actually installable after setbacks, "
            "obstructions and self-shading. DEFAULT 0.75 is a modelling assumption "
            "for a drawn usable roof; cf. NREL Gagnon 2016 reports only ~26% of "
            "TOTAL small-building rooftop stock is suitable — a different measure."
        ),
    )
    module_efficiency: float = Field(
        default=0.20,
        gt=0.0,
        le=0.27,
        description=(
            "STC module efficiency (fraction). 0.20 ≈ premium silicon today "
            "(PVWatts premium 19%, NREL Gagnon 16-20%). kWp/m² equals this value."
        ),
    )


class ConsumptionInput(BaseModel):
    """Household electricity use. The user supplies real numbers; nothing stubbed."""

    annual_kwh: float | None = Field(
        default=None, ge=0.0, description="Annual household consumption (kWh)."
    )
    self_consumption_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of PV generation consumed on-site rather than exported. "
            "If None, an annual-net-metering balance is used instead (export = "
            "production above annual consumption). Policy- and load-shape-dependent."
        ),
    )


class EconomicInputs(BaseModel):
    """Economic inputs. Money-valued fields default to None = NEEDS_HUMAN_DECISION.

    Recommended RANGES (not values) are documented in `_pm/MORNING_QUEUE.md` and
    on :data:`solarsite.consumer.RECOMMENDED_RANGES`. We do not fill these in.
    """

    install_cost_usd_per_w: float | None = Field(
        default=None, gt=0.0, description="Installed cost USD/W_DC. NEEDS_HUMAN_DECISION."
    )
    retail_tariff_usd_per_kwh: float | None = Field(
        default=None, ge=0.0, description="Retail electricity price USD/kWh. NEEDS_HUMAN_DECISION."
    )
    export_rate_usd_per_kwh: float | None = Field(
        default=None,
        ge=0.0,
        description="Feed-in / net-export credit USD/kWh. NEEDS_HUMAN_DECISION (policy).",
    )
    incentive_usd: float | None = Field(
        default=None, ge=0.0, description="Upfront rebate/credit USD. NEEDS_HUMAN_DECISION."
    )
    om_cost_usd_per_kw_yr: float | None = Field(
        default=None, ge=0.0, description="Annual O&M USD/kW/yr. NEEDS_HUMAN_DECISION."
    )
    discount_rate: float = Field(
        default=0.05, ge=0.0, le=0.5, description="Real discount rate for NPV (dimensionless)."
    )
    analysis_years: int = Field(default=25, ge=1, le=40, description="Economic horizon (years).")
    degradation_per_yr: float = Field(
        default=0.005, ge=0.0, le=0.05, description="Annual production degradation (fraction)."
    )


class EnergyBalance(BaseModel):
    """The real, computed energy outcome (no stubs)."""

    capacity_kwp: float
    specific_yield_kwh_kwp_yr: float
    annual_production_kwh: float
    self_consumed_kwh: float
    exported_kwh: float
    grid_import_kwh: float
    self_consumption_ratio: float = Field(..., description="self_consumed / production.")
    self_sufficiency: float = Field(..., description="self_consumed / consumption (0 if no use).")
    dispatch_policy: str


class EconomicsResult(BaseModel):
    """Monetary outcome. Any field may be None when a required input is unverified."""

    install_cost_usd: float | None = None
    net_install_cost_usd: float | None = None
    annual_savings_usd: float | None = None
    simple_payback_years: float | None = None
    npv_usd: float | None = None
    lifetime_savings_usd: float | None = None
    unverified_inputs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class ConsumerResult(BaseModel):
    """Full consumer-mode result: real energy + (possibly stubbed) economics."""

    energy: EnergyBalance
    economics: EconomicsResult
    sanity_ok: bool = Field(..., description="True iff every physical-sanity check passed.")
    sanity_messages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list, description="Human-readable ledger of every assumption used."
    )


class RooftopAnalysisRequest(BaseModel):
    """Request body for POST /consumer/rooftop.

    ``specific_yield_kwh_kwp_yr`` is supplied by the caller (sourced from the PV
    energy engine for the roof's location) — the consumer mode reuses that
    validated yield rather than re-deriving PV physics.
    """

    roof: RoofInput
    specific_yield_kwh_kwp_yr: float = Field(
        ..., gt=0.0, le=3000.0, description="Site specific yield (kWh/kWp/yr)."
    )
    consumption: ConsumptionInput | None = None
    economics: EconomicInputs | None = None
