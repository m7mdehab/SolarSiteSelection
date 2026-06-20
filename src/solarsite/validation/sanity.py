"""Physical-sanity bounds (Track F) — make an insane number impossible to ship.

Every headline quantity has a HARD physical/market envelope. A value outside it
is, with near-certainty, a modelling or units bug — not a real result — so it is
flagged (and, where a gate calls :func:`assert_within`, blocked) rather than
displayed. These are *plausibility envelopes*, deliberately wide; they are NOT
precise point estimates and must never be read as "the expected value."

Provenance of the envelopes (each verified against its cited source):

* Specific yield — World Bank/ESMAP/Solargis *Global Photovoltaic Power Potential
  by Country* (2020): 93 % of population lives where daily PVOUT is 3.0-5.0
  kWh/kWp (≈1,095-1,825 kWh/kWp/yr); best countries (Namibia) ~6 kWh/kWp/day.
  Generic specific-yield band 1,000-2,000+ kWh/kWp/yr (Solar Power World). Hard
  fixed-tilt ceiling ~2,200; we use [500, 2600] as the *certainly-a-bug* envelope.
* AC capacity factor — LBNL *Utility-Scale Solar, 2023 Edition*: plant CF 9-35 %
  (AC basis), median 24 % (sample mixes fixed-tilt + tracking). NREL ATB 2024
  utility tracking up to ~34 %. Fixed-tilt typically 10-28 %. Hard [0.02, 0.40].
* Power density — NREL *Land-Use Requirements for Solar* (Ong et al. 2013,
  NREL/TP-6A20-56290): ~28 MWac/km² (total area) to ~34 MWac/km² (direct area);
  modern dense DC layouts ~40-50 MWac/km². Hard [2, 100] MWp/km².
* Residential install cost — NREL *Documenting 15 Years of Reductions in U.S.
  Solar PV System Costs* (Ramasamy et al. 2025, NREL/TP-7A40-92536): 2024 = $3.25/Wdc,
  series low $2.87 (2023), 2009 high $9.23. LBNL *Tracking the Sun 2024*: 20th-80th
  percentile $3.2-$5.5/W (residential, 2023). Hard [0.7, 12] $/W.
* Module efficiency — PVWatts V5 (Dobos 2014): standard 15 %, premium 19 %;
  NREL Gagnon 2016 used 16 %; premium today ~20-22 %. Commercial modules do not
  exceed ~25 %. Hard [0.05, 0.27]; the 0.25 ceiling bounds per-roof capacity.
* LCOE / payback envelopes are engineering plausibility limits, not sourced
  market figures — wide enough that only a bug falls outside.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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

#: Hours in a (non-leap) year. 1 kWp at capacity factor 1.0 makes 8760 kWh/yr.
HOURS_PER_YEAR: float = 8760.0

#: Hard ceiling on installable DC capacity per m² of module area: 25 % efficient
#: modules at 1000 W/m² STC = 0.25 kWp/m². Commercial modules do not exceed this.
MAX_MODULE_KWP_PER_M2: float = 0.25


class Bound(BaseModel):
    """A hard physical/market plausibility envelope for one named quantity."""

    name: str
    lo: float
    hi: float
    unit: str
    source: str = Field(..., description="Where the envelope comes from (short).")
    note: str = ""

    def contains(self, value: float) -> bool:
        return self.lo <= float(value) <= self.hi


class SanityCheck(BaseModel):
    """Result of checking one value against its :class:`Bound`."""

    name: str
    value: float
    ok: bool
    lo: float
    hi: float
    unit: str
    message: str = ""


class SanityViolation(ValueError):
    """Raised by :func:`assert_within` when a value escapes its hard envelope."""


# ---------------------------------------------------------------------------
# The registry of hard envelopes. WIDE on purpose — only a bug falls outside.
# ---------------------------------------------------------------------------
PHYSICAL_BOUNDS: dict[str, Bound] = {
    "specific_yield_kwh_kwp_yr": Bound(
        name="specific_yield_kwh_kwp_yr",
        lo=500.0,
        hi=2600.0,
        unit="kWh/kWp/yr",
        source="World Bank/ESMAP/Solargis GSA 2020; NREL; Solar Power World",
        note="Typical 900-2200; fixed-tilt hard ceiling ~2200. Outside [500,2600] => bug.",
    ),
    "ac_capacity_factor": Bound(
        name="ac_capacity_factor",
        lo=0.02,
        hi=0.40,
        unit="fraction",
        source="LBNL Utility-Scale Solar 2023 (9-35%, median 24%); NREL ATB 2024",
        note="Fixed-tilt typically 0.10-0.28; tracking up to ~0.34.",
    ),
    "power_density_mwp_per_km2": Bound(
        name="power_density_mwp_per_km2",
        lo=2.0,
        hi=100.0,
        unit="MWp/km2",
        source="NREL Ong et al. 2013 (NREL/TP-6A20-56290); modern dense DC ~40-50",
        note="Project default 45 MWp/km2 sits inside. Outside [2,100] => bug.",
    ),
    "lcoe_usd_per_mwh": Bound(
        name="lcoe_usd_per_mwh",
        lo=10.0,
        hi=600.0,
        unit="USD/MWh",
        source="engineering/market plausibility envelope (not a sourced point figure)",
        note="Utility PV typically 20-150 USD/MWh; envelope deliberately wide.",
    ),
    "residential_install_cost_usd_per_w": Bound(
        name="residential_install_cost_usd_per_w",
        lo=0.7,
        hi=12.0,
        unit="USD/W_DC",
        source="NREL/TP-7A40-92536 (2024=$3.25, 2009=$9.23); LBNL TTS 2024 ($3.2-5.5)",
        note="Typical residential 2.5-6.5 $/W. Outside [0.7,12] => bug.",
    ),
    "payback_years": Bound(
        name="payback_years",
        lo=0.5,
        hi=60.0,
        unit="years",
        source="engineering plausibility envelope (not a sourced point figure)",
        note="Residential PV simple payback typically 4-25 yr.",
    ),
    "module_efficiency_fraction": Bound(
        name="module_efficiency_fraction",
        lo=0.05,
        hi=0.27,
        unit="fraction",
        source="PVWatts V5 (std 15%, prem 19%); NREL Gagnon 2016 (16%); commercial <=~25%",
        note="Lab cells exceed this; commercial modules do not.",
    ),
}


def capacity_factor_from_specific_yield(specific_yield_kwh_kwp_yr: float) -> float:
    """AC capacity factor implied by a specific yield: ``sy / 8760``.

    1 kWp running at CF=1 for a year produces 8760 kWh, so the dimensionless
    capacity factor is simply the specific yield divided by the hours in a year.
    """
    return float(specific_yield_kwh_kwp_yr) / HOURS_PER_YEAR


def check(name: str, value: float) -> SanityCheck:
    """Check one value against its registered hard envelope."""
    if name not in PHYSICAL_BOUNDS:
        raise KeyError(f"No physical bound registered for '{name}'")
    b = PHYSICAL_BOUNDS[name]
    ok = b.contains(value)
    msg = (
        ""
        if ok
        else f"{name}={value:.4g} {b.unit} outside physical envelope "
        f"[{b.lo:g}, {b.hi:g}] (source: {b.source})"
    )
    return SanityCheck(
        name=name, value=float(value), ok=ok, lo=b.lo, hi=b.hi, unit=b.unit, message=msg
    )


def check_many(values: dict[str, float]) -> list[SanityCheck]:
    """Check a mapping of name -> value, skipping names with no registered bound."""
    return [check(n, v) for n, v in values.items() if n in PHYSICAL_BOUNDS]


def assert_within(name: str, value: float) -> None:
    """Raise :class:`SanityViolation` if ``value`` escapes its hard envelope."""
    c = check(name, value)
    if not c.ok:
        raise SanityViolation(c.message)


def check_roof_capacity(roof_area_m2: float, capacity_kwp: float) -> SanityCheck:
    """Per-roof capacity must not exceed 0.25 kWp/m² (25 %-efficient modules).

    This is the bound that makes an over-stated rooftop capacity impossible: no
    real installation packs more than ~250 W of modules per m² of roof.
    """
    ceiling = float(roof_area_m2) * MAX_MODULE_KWP_PER_M2
    ok = float(capacity_kwp) <= ceiling + 1e-9
    msg = (
        ""
        if ok
        else f"capacity={capacity_kwp:.4g} kWp exceeds physical roof ceiling "
        f"{ceiling:.4g} kWp ({MAX_MODULE_KWP_PER_M2} kWp/m2 x {roof_area_m2:.4g} m2)"
    )
    return SanityCheck(
        name="roof_capacity_kwp",
        value=float(capacity_kwp),
        ok=ok,
        lo=0.0,
        hi=ceiling,
        unit="kWp",
        message=msg,
    )


def check_energy_result(
    specific_yield_kwh_kwp_yr: float,
    *,
    area_km2: float | None = None,
    capacity_mwp: float | None = None,
    lcoe_usd_per_mwh: float | None = None,
) -> list[SanityCheck]:
    """Run every applicable physical-sanity check on an energy result.

    Always checks specific yield and the capacity factor *derived* from it.
    When ``area_km2`` and ``capacity_mwp`` are both given, also checks the
    implied power density (MWp/km²). When ``lcoe_usd_per_mwh`` is given, checks
    it too. Returns one :class:`SanityCheck` per evaluated quantity.
    """
    checks = [
        check("specific_yield_kwh_kwp_yr", specific_yield_kwh_kwp_yr),
        check(
            "ac_capacity_factor",
            capacity_factor_from_specific_yield(specific_yield_kwh_kwp_yr),
        ),
    ]
    if area_km2 is not None and capacity_mwp is not None and area_km2 > 0:
        checks.append(check("power_density_mwp_per_km2", float(capacity_mwp) / float(area_km2)))
    if lcoe_usd_per_mwh is not None:
        checks.append(check("lcoe_usd_per_mwh", lcoe_usd_per_mwh))
    return checks
