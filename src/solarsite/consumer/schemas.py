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
  tariff, export/feed-in rate, incentives. Those fields are user-provided and
  default to ``None``. When a required input is ``None`` the dependent output is
  ``None`` and a caveat is emitted; we never invent a value to fill it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "CO2Result",
    "CashflowPoint",
    "ConsumerResult",
    "ConsumptionInput",
    "EconomicInputs",
    "EconomicsResult",
    "EnergyBalance",
    "ProductionDetail",
    "RoofInput",
    "RooftopAnalysisRequest",
    "UncertaintyBand",
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
            "If None, the dispatch policy + load archetype are used instead. "
            "Policy- and load-shape-dependent."
        ),
    )
    load_profile: str | None = Field(
        default=None,
        description="Load archetype for diurnal matching: 'flat' | 'daytime' | 'evening'.",
    )
    dispatch_policy: str | None = Field(
        default=None,
        description="'net_metering' | 'self_consumption' | 'no_export'. None -> net_metering.",
    )


class EconomicInputs(BaseModel):
    """Economic inputs. Money-valued fields are user-provided and default to None.

    Recommended RANGES (not values) with sources are exposed on
    :data:`solarsite.consumer.RECOMMENDED_RANGES`. We do not fill these in.
    """

    install_cost_usd_per_w: float | None = Field(
        default=None, gt=0.0, description="Installed cost USD/W_DC. Enter your own quote."
    )
    retail_tariff_usd_per_kwh: float | None = Field(
        default=None,
        ge=0.0,
        description="Retail electricity price USD/kWh. Enter the rate from your bill.",
    )
    export_rate_usd_per_kwh: float | None = Field(
        default=None,
        ge=0.0,
        description="Feed-in / net-export credit USD/kWh. Depends on your local policy.",
    )
    incentive_usd: float | None = Field(
        default=None, ge=0.0, description="Upfront rebate/credit USD. Enter if you qualify."
    )
    om_cost_usd_per_kw_yr: float | None = Field(
        default=None, ge=0.0, description="Annual O&M USD/kW/yr. Enter your own estimate."
    )
    grid_co2_g_per_kwh: float | None = Field(
        default=None,
        ge=0.0,
        le=2000.0,
        description="Grid carbon intensity gCO2/kWh for CO2 avoided. None -> not shown.",
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


class CashflowPoint(BaseModel):
    """One year of the project cashflow (for the payback/cashflow curve)."""

    year: int
    annual_cash_usd: float
    cumulative_usd: float


class EconomicsResult(BaseModel):
    """Monetary outcome. Any field may be None when a required input is unverified."""

    install_cost_usd: float | None = None
    net_install_cost_usd: float | None = None
    annual_savings_usd: float | None = None
    simple_payback_years: float | None = None
    npv_usd: float | None = None
    irr_pct: float | None = Field(default=None, description="Internal rate of return (%) or None.")
    lifetime_savings_usd: float | None = None
    cashflow: list[CashflowPoint] = Field(
        default_factory=list, description="Per-year discounted-free cashflow incl. year 0 (cost)."
    )
    unverified_inputs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class CO2Result(BaseModel):
    """CO2 avoided from a USER-PROVIDED grid factor (never a fabricated default)."""

    grid_factor_g_per_kwh: float | None = None
    annual_kg: float | None = None
    lifetime_kg: float | None = None
    basis: str = ""
    note: str = ""


class UncertaintyBand(BaseModel):
    """low / base / high band for one figure, with what drives it."""

    low: float
    base: float
    high: float
    basis: str = Field(..., description="What the band reflects (e.g. production model spread).")


class ProductionDetail(BaseModel):
    """Phase B: the user's real orientation vs the optimum + interannual P50/P90.

    Lets the consumer see whether their roof faces the right way and how confident
    to be year-to-year — without any manufactured numbers (P90 is ``None`` when the
    interannual source is unreachable).
    """

    surface_tilt: float = Field(..., description="Tilt used (deg) — the user's roof.")
    surface_azimuth: float = Field(..., description="Azimuth used (deg CW from N).")
    optimal_tilt: float
    optimal_azimuth: float
    optimal_specific_yield_kwh_kwp_yr: float
    orientation_ratio: float = Field(..., description="yours / optimal (0..1).")
    shading_pct: float = Field(..., description="Shading loss applied (replaces the flat 3%).")
    p50_specific_yield_kwh_kwp_yr: float
    p90_specific_yield_kwh_kwp_yr: float | None = None
    interannual_note: str = ""


class ConsumerResult(BaseModel):
    """Full consumer-mode result: real energy + (possibly stubbed) economics."""

    energy: EnergyBalance
    economics: EconomicsResult
    sanity_ok: bool = Field(..., description="True iff every physical-sanity check passed.")
    sanity_messages: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list, description="Human-readable ledger of every assumption used."
    )
    # Validation-grade monthly production profile (when computed from a location).
    monthly_kwh: list[float] | None = Field(
        default=None, description="12 monthly production values (kWh) for the system; Jan..Dec."
    )
    production_method: str | None = Field(
        default=None, description="'pvlib_modelchain' (validation-grade) or 'caller_supplied'."
    )
    production_note: str | None = Field(
        default=None, description="Honest caveat about the production figure's uncertainty."
    )
    payback_band: UncertaintyBand | None = Field(
        default=None, description="Uncertainty band on simple payback, when computable."
    )
    unverified_panel: list[str] = Field(
        default_factory=list,
        description="'What we can't verify for your area' — every unsourced/missing input.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Friendly plausibility warnings (e.g. roof larger than a typical building).",
    )
    production_detail: ProductionDetail | None = Field(
        default=None,
        description="Orientation-vs-optimum + interannual P50/P90 (when location-derived).",
    )
    co2: CO2Result | None = Field(
        default=None, description="CO2 avoided (only when the user supplied a grid factor)."
    )


class RooftopAnalysisRequest(BaseModel):
    """Request body for POST /consumer/rooftop.

    Supply EITHER ``specific_yield_kwh_kwp_yr`` directly, OR ``latitude`` +
    ``longitude`` so the server computes a validation-grade yield (pvlib ModelChain
    on the PVGIS TMY for that point). Lat/lon takes precedence when both are given.
    """

    roof: RoofInput
    specific_yield_kwh_kwp_yr: float | None = Field(
        default=None, gt=0.0, le=3000.0, description="Caller-supplied specific yield (kWh/kWp/yr)."
    )
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    surface_tilt: float | None = Field(
        default=None, ge=0.0, le=90.0, description="Roof tilt (deg); None -> |latitude| optimum."
    )
    surface_azimuth: float | None = Field(
        default=None, ge=0.0, le=360.0, description="Roof azimuth (deg CW from N); None -> equator."
    )
    shading_pct: float | None = Field(
        default=None, ge=0.0, le=80.0, description="Shading loss %; None -> model default (3%)."
    )
    consumption: ConsumptionInput | None = None
    economics: EconomicInputs | None = None
