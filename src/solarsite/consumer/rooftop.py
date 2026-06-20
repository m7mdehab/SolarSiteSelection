"""Consumer rooftop-PV engine (Track B).

Pure, deterministic functions. The energy side is fully computed; the economic
side returns ``None`` for any output whose required input is an unverified
``NEEDS_HUMAN_DECISION`` stub (never a fabricated number). Every output is run
through the physical-sanity gate before it leaves this module.
"""

from __future__ import annotations

from solarsite.consumer.schemas import (
    ConsumerResult,
    ConsumptionInput,
    EconomicInputs,
    EconomicsResult,
    EnergyBalance,
    RoofInput,
    UncertaintyBand,
)
from solarsite.uncertainty import propagate
from solarsite.validation import check, check_energy_result, check_roof_capacity

__all__ = [
    "analyze_rooftop",
    "annual_production_kwh",
    "compute_economics",
    "energy_balance",
    "roof_capacity_kwp",
]


def roof_capacity_kwp(roof: RoofInput) -> float:
    """Installable DC capacity (kWp) from roof area x usable fraction x efficiency.

    At STC (1000 W/m²) a module of efficiency η delivers η kWp per m², so
    ``capacity = area x usable_fraction x η``. This derives capacity from the
    physical roof — it CANNOT yield a land-packing-density "carpet the region"
    figure.
    """
    return float(roof.area_m2) * float(roof.usable_fraction) * float(roof.module_efficiency)


def annual_production_kwh(capacity_kwp: float, specific_yield_kwh_kwp_yr: float) -> float:
    """Annual generation (kWh) = capacity (kWp) x specific yield (kWh/kWp/yr)."""
    return float(capacity_kwp) * float(specific_yield_kwh_kwp_yr)


def energy_balance(
    capacity_kwp: float,
    specific_yield_kwh_kwp_yr: float,
    consumption: ConsumptionInput,
) -> EnergyBalance:
    """Split annual production into self-consumed / exported / grid-import.

    Two dispatch policies (the LOGIC is autonomous; which policy a jurisdiction
    allows is a NEEDS_HUMAN_DECISION surfaced to the user):

    * ``self_consumption_fraction`` given → instantaneous self-consumption: a
      fixed fraction of production is used on-site (capped at consumption).
    * otherwise → annual net metering: everything up to annual consumption is
      self-consumed; the surplus is exported.
    """
    production = annual_production_kwh(capacity_kwp, specific_yield_kwh_kwp_yr)
    cons = float(consumption.annual_kwh) if consumption.annual_kwh is not None else 0.0

    if consumption.self_consumption_fraction is not None:
        policy = "instantaneous_self_consumption"
        self_consumed = min(production * float(consumption.self_consumption_fraction), cons)
        if cons == 0.0:
            self_consumed = 0.0
    else:
        policy = "annual_net_metering"
        self_consumed = min(production, cons)

    exported = max(0.0, production - self_consumed)
    grid_import = max(0.0, cons - self_consumed)
    scr = self_consumed / production if production > 0 else 0.0
    self_sufficiency = self_consumed / cons if cons > 0 else 0.0

    return EnergyBalance(
        capacity_kwp=round(capacity_kwp, 4),
        specific_yield_kwh_kwp_yr=round(specific_yield_kwh_kwp_yr, 1),
        annual_production_kwh=round(production, 1),
        self_consumed_kwh=round(self_consumed, 1),
        exported_kwh=round(exported, 1),
        grid_import_kwh=round(grid_import, 1),
        self_consumption_ratio=round(scr, 4),
        self_sufficiency=round(self_sufficiency, 4),
        dispatch_policy=policy,
    )


def compute_economics(balance: EnergyBalance, econ: EconomicInputs) -> EconomicsResult:
    """Residential economics. Any output whose input is a stub stays ``None``.

    No value is invented: if ``install_cost_usd_per_w`` is unverified, there is
    no install cost; if ``retail_tariff_usd_per_kwh`` is unverified, there are no
    savings; and so on. Each missing input is listed and surfaced as a caveat.
    """
    unverified: list[str] = []
    caveats: list[str] = []

    def _need(field: str, value: float | None) -> float | None:
        if value is None:
            unverified.append(field)
            caveats.append(f"estimate not available — '{field}' is not verified for your area")
        return value

    cost_per_w = _need("install_cost_usd_per_w", econ.install_cost_usd_per_w)
    tariff = _need("retail_tariff_usd_per_kwh", econ.retail_tariff_usd_per_kwh)
    export_rate = econ.export_rate_usd_per_kwh
    if export_rate is None:
        unverified.append("export_rate_usd_per_kwh")
        caveats.append("export credit not applied — 'export_rate_usd_per_kwh' is not verified")
    incentive = econ.incentive_usd
    if incentive is None:
        unverified.append("incentive_usd")
        caveats.append("incentives not applied — 'incentive_usd' is not verified for your area")
    om = econ.om_cost_usd_per_kw_yr
    if om is None:
        unverified.append("om_cost_usd_per_kw_yr")

    result = EconomicsResult(unverified_inputs=unverified, caveats=caveats)

    # Install cost (needs $/W only).
    if cost_per_w is not None:
        install = balance.capacity_kwp * 1000.0 * cost_per_w
        result.install_cost_usd = round(install, 2)
        result.net_install_cost_usd = round(install - (incentive or 0.0), 2)

    # Annual savings (needs retail tariff; export + O&M are added if available).
    if tariff is not None:
        savings = balance.self_consumed_kwh * tariff
        if export_rate is not None:
            savings += balance.exported_kwh * export_rate
        if om is not None:
            savings -= balance.capacity_kwp * om
        result.annual_savings_usd = round(savings, 2)

    # Payback / NPV / lifetime savings (need both cost and savings).
    net_cost = result.net_install_cost_usd
    annual_savings = result.annual_savings_usd
    if net_cost is not None and annual_savings is not None and annual_savings > 0:
        result.simple_payback_years = round(net_cost / annual_savings, 2)
        r = econ.discount_rate
        deg = econ.degradation_per_yr
        npv = -net_cost
        lifetime = 0.0
        for t in range(1, econ.analysis_years + 1):
            cash = annual_savings * (1.0 - deg) ** (t - 1)
            lifetime += cash
            npv += cash / (1.0 + r) ** t
        result.npv_usd = round(npv, 2)
        result.lifetime_savings_usd = round(lifetime, 2)

    return result


def analyze_rooftop(
    roof: RoofInput,
    specific_yield_kwh_kwp_yr: float,
    consumption: ConsumptionInput | None = None,
    econ: EconomicInputs | None = None,
    *,
    monthly_kwh_per_kwp: list[float] | None = None,
    production_method: str = "caller_supplied",
) -> ConsumerResult:
    """End-to-end consumer-mode analysis: roof → capacity → energy → economics.

    ``specific_yield_kwh_kwp_yr`` comes from the SAME validated energy engine the
    utility mode uses (pvlib ModelChain, or the labelled offline estimate) — this
    module does not re-derive PV physics. Returns a :class:`ConsumerResult` with
    real energy figures, possibly-stubbed economics, an assumptions ledger, the
    physical-sanity verdict, an optional monthly profile, a payback uncertainty
    band, and a "what we can't verify" panel.
    """
    consumption = consumption or ConsumptionInput()
    econ = econ or EconomicInputs()

    capacity = roof_capacity_kwp(roof)
    balance = energy_balance(capacity, specific_yield_kwh_kwp_yr, consumption)
    economics = compute_economics(balance, econ)

    # ---- physical-sanity gate -------------------------------------------------
    checks = [check_roof_capacity(roof.area_m2, capacity)]
    checks.extend(check_energy_result(specific_yield_kwh_kwp_yr))
    checks.append(check("module_efficiency_fraction", roof.module_efficiency))
    if economics.simple_payback_years is not None:
        checks.append(check("payback_years", economics.simple_payback_years))
    if econ.install_cost_usd_per_w is not None:
        checks.append(check("residential_install_cost_usd_per_w", econ.install_cost_usd_per_w))
    sanity_messages = [c.message for c in checks if not c.ok]
    sanity_ok = not sanity_messages

    # ---- assumptions ledger ---------------------------------------------------
    ledger = [
        f"usable roof fraction = {roof.usable_fraction} (modelling assumption for a drawn roof)",
        f"module efficiency = {roof.module_efficiency} (premium silicon, STC kWp/m²)",
        f"dispatch policy = {balance.dispatch_policy}",
        f"discount rate = {econ.discount_rate}; degradation = {econ.degradation_per_yr}/yr; "
        f"horizon = {econ.analysis_years} yr",
        f"specific yield = {specific_yield_kwh_kwp_yr} kWh/kWp/yr (from the PV energy engine)",
    ]
    if economics.unverified_inputs:
        ledger.append(
            "UNVERIFIED economic inputs (NEEDS_HUMAN_DECISION): "
            + ", ".join(economics.unverified_inputs)
        )

    # ---- monthly system profile (kWh) ----------------------------------------
    monthly_kwh: list[float] | None = None
    if monthly_kwh_per_kwp is not None:
        monthly_kwh = [round(m * capacity, 1) for m in monthly_kwh_per_kwp]

    # ---- production uncertainty note -----------------------------------------
    if production_method == "pvlib_modelchain":
        production_note = (
            "Production is validation-grade (pvlib ModelChain on the PVGIS TMY for "
            "your location; agrees with the PVGIS oracle to within ~2-4%). It does "
            "NOT yet include year-to-year (interannual) variability (~+/-5-10%), so "
            "treat it as a typical-year P50 estimate."
        )
    else:
        production_note = (
            "Production uses a caller-supplied specific yield; for a validation-grade "
            "figure provide a location (latitude/longitude)."
        )

    # ---- payback uncertainty band (Track C) ----------------------------------
    payback_band: UncertaintyBand | None = None
    net = economics.net_install_cost_usd
    savings = economics.annual_savings_usd
    if economics.simple_payback_years is not None and net is not None and savings and savings > 0:
        # Propagate the production model spread (+/-4%, the measured oracle bound)
        # through savings -> payback. Lower production -> lower savings -> longer payback.
        band = propagate(
            lambda annual_savings: net / annual_savings,
            {"annual_savings": (savings * 0.96, savings, savings * 1.04)},
        )
        payback_band = UncertaintyBand(
            low=round(band.low, 2),
            base=round(band.base, 2),
            high=round(band.high, 2),
            basis=(
                "production model spread (+/-4% vs PVGIS oracle); excludes interannual "
                "variability and economic-input (cost/tariff) uncertainty"
            ),
        )

    # ---- "what we can't verify for your area" panel --------------------------
    panel = [
        f"{name}: not verified for your area (enter your own value)"
        for name in economics.unverified_inputs
    ]

    return ConsumerResult(
        energy=balance,
        economics=economics,
        sanity_ok=sanity_ok,
        sanity_messages=sanity_messages,
        assumptions=ledger,
        monthly_kwh=monthly_kwh,
        production_method=production_method,
        production_note=production_note,
        payback_band=payback_band,
        unverified_panel=panel,
    )
