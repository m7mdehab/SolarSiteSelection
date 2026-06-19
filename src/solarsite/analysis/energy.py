"""Energy yield and economics estimation for candidate PV sites (P2.4).

Uses pvlib ModelChain with a PVWatts-style (single-diode-free) setup to
simulate annual AC energy yield from a Typical Meteorological Year (TMY)
DataFrame fetched by :mod:`solarsite.acquire.pvgis`.

Model choices
-------------
* **Mount**: Fixed-tilt at surface_tilt = site latitude (if not overridden),
  surface_azimuth = 180° (due south, northern-hemisphere default).
* **Module**: 1 kWp reference normalised module -- ``pdc0=1000 W``,
  ``gamma_pdc=-0.004`` (%/°C).
* **Inverter**: PVWatts-style single-parameter inverter with ``eta_inv_nom``
  set to **0.96** (documents the 4 % DC-to-AC derate / system losses).
* **Temperature model**: SAPM open-rack glass-glass coefficients.
* **AOI model**: ``'physical'`` (no extra reflection losses beyond Fresnel).
* **Spectral model**: ``'no_loss'`` (spectral correction skipped for speed).

The 0.96 ``eta_inv_nom`` is the single place that encodes "system losses"
(inverter efficiency + DC wiring + mismatch + soiling margin).

LCOE formula
------------
  CRF  = r * (1+r)^n / ((1+r)^n - 1)          # capital recovery factor
  LCOE = (capex_per_kwp * CRF + opex_per_kwp_yr) / specific_yield_kwh_kwp_yr
         * 1000                                 # convert USD/kWh → USD/MWh

where r = discount_rate, n = lifetime_yr.

Default economic assumptions
-----------------------------
* packing_density_mwp_per_km2 = 45 MWp/km² (ground-mounted utility-scale PV
  at ~20% land-use factor with ~22% module efficiency + spacing).
* capex_per_kwp = 1000 USD/kWp (2024 utility-scale benchmark).
* opex_per_kwp_yr = 17 USD/kWp/yr (O&M + insurance, ~1.7 % of CAPEX).
* discount_rate = 0.07 (7 % real WACC).
* lifetime_yr = 25 years.
"""

from __future__ import annotations

import logging
from typing import cast

import pandas as pd
import pvlib
from pydantic import BaseModel, Field

__all__ = [
    "EnergyAssumptions",
    "EnergyResult",
    "site_energy",
    "specific_yield",
]

log = logging.getLogger(__name__)

# PVGIS column names -> pvlib weather column names
_PVGIS_RENAME: dict[str, str] = {
    "G(h)": "ghi",
    "Gb(n)": "dni",
    "Gd(h)": "dhi",
    "T2m": "temp_air",
    "WS10m": "wind_speed",
}

# Temperature model coefficients for open-rack glass-glass module
_TEMP_MODEL_PARAMS = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_glass"]


class EnergyAssumptions(BaseModel):
    """Configurable assumptions for energy and economics calculations.

    Attributes
    ----------
    tilt:
        Panel tilt angle in degrees from horizontal.  ``None`` (default) means
        use the site latitude as the optimal fixed tilt.
    azimuth:
        Panel azimuth in degrees clockwise from North.  180° = due south,
        which is optimal for the northern hemisphere.
    dc_ac_ratio:
        DC-to-AC ratio (inverter loading ratio).  Currently informational;
        the 0.96 derate is applied via the inverter ``eta_inv_nom`` parameter.
    packing_density_mwp_per_km2:
        Nameplate DC capacity per unit land area (MWp/km²).  Default 45 MWp/km²
        reflects ground-mounted utility-scale PV at ~20% land coverage with
        modern bifacial modules and typical row spacing.
    capex_per_kwp:
        Overnight capital cost in USD per kWp-DC nameplate.
    opex_per_kwp_yr:
        Annual operating cost in USD per kWp-DC nameplate (O&M + insurance).
    discount_rate:
        Real weighted-average cost of capital (dimensionless, e.g. 0.07 = 7%).
    lifetime_yr:
        Project economic life in years.
    """

    tilt: float | None = Field(default=None, description="Panel tilt (degrees); None -> latitude")
    azimuth: float = Field(default=180.0, description="Panel azimuth (degrees, 180=due south)")
    dc_ac_ratio: float = Field(
        default=1.0,
        description="DC/AC ratio (informational; derate applied via eta_inv_nom=0.96)",
    )
    packing_density_mwp_per_km2: float = Field(
        default=45.0,
        description="Nameplate DC capacity per km² of land (MWp/km²)",
    )
    capex_per_kwp: float = Field(default=1000.0, description="CAPEX in USD/kWp")
    opex_per_kwp_yr: float = Field(default=17.0, description="Annual OPEX in USD/kWp/yr")
    discount_rate: float = Field(default=0.07, description="Real WACC (dimensionless)")
    lifetime_yr: int = Field(default=25, description="Project lifetime in years")


class EnergyResult(BaseModel):
    """Output of a site-level energy and LCOE calculation.

    Attributes
    ----------
    specific_yield_kwh_kwp_yr:
        Annual AC energy per unit of installed DC capacity (kWh/kWp/yr).
    capacity_mwp:
        Total DC nameplate capacity installed on the site (MWp).
    annual_gwh:
        Annual AC electricity generation (GWh/yr).
    lcoe_usd_per_mwh:
        Levelised Cost of Energy (USD/MWh).
    assumptions:
        The :class:`EnergyAssumptions` used to produce this result.
    """

    specific_yield_kwh_kwp_yr: float
    capacity_mwp: float
    annual_gwh: float
    lcoe_usd_per_mwh: float
    assumptions: EnergyAssumptions


def _prepare_weather(tmy_df: pd.DataFrame) -> pd.DataFrame:
    """Rename PVGIS columns to pvlib standard names and return a clean copy.

    Accepts both PVGIS raw column names (e.g. ``G(h)``, ``T2m``) and pvlib
    standard names (e.g. ``ghi``, ``temp_air``) so the function is idempotent
    when called with already-renamed DataFrames.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns ``ghi``, ``dni``, ``dhi``, ``temp_air``,
        ``wind_speed`` and a timezone-aware DatetimeIndex.
    """
    df = tmy_df.copy()

    # Rename PVGIS columns that are present
    rename_map = {k: v for k, v in _PVGIS_RENAME.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    required = {"ghi", "dni", "dhi", "temp_air", "wind_speed"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"TMY DataFrame is missing required columns after renaming: {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    # Ensure the index is timezone-aware (pvlib requires tz-aware datetimes)
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    cols = sorted(required)  # deterministic column order
    return cast(pd.DataFrame, df[cols].copy())


def _build_system(
    tilt: float,
    azimuth: float,
) -> pvlib.pvsystem.PVSystem:
    """Build a 1 kWp reference PVSystem for the given tilt/azimuth.

    The system is normalised to 1 kWp (pdc0=1000 W) so that ``mc.results.ac``
    is directly in W per kWp, and the annual sum is kWh/kWp.

    The inverter ``eta_inv_nom=0.96`` encodes a 4 % system-losses derate
    (inverter efficiency + DC wiring + mismatch + soiling).
    """
    mount = pvlib.pvsystem.FixedMount(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
    )
    array = pvlib.pvsystem.Array(
        mount=mount,
        module_parameters={"pdc0": 1000.0, "gamma_pdc": -0.004},
        temperature_model_parameters=_TEMP_MODEL_PARAMS,
    )
    system = pvlib.pvsystem.PVSystem(
        arrays=[array],
        inverter_parameters={"pdc0": 1000.0, "eta_inv_nom": 0.96},
    )
    return system


def specific_yield(
    lat: float,
    lon: float,
    tmy_df: pd.DataFrame,
    assumptions: EnergyAssumptions,
) -> float:
    """Estimate annual specific yield (kWh/kWp/yr) for a location.

    Runs pvlib :class:`~pvlib.modelchain.ModelChain` on the provided TMY
    weather data and returns the AC energy sum normalised to 1 kWp of installed
    DC capacity.

    Parameters
    ----------
    lat:
        Site latitude in decimal degrees (positive = North).
    lon:
        Site longitude in decimal degrees (positive = East).
    tmy_df:
        Hourly TMY DataFrame from PVGIS (or any source) with columns
        ``G(h)``/``ghi``, ``Gb(n)``/``dni``, ``Gd(h)``/``dhi``,
        ``T2m``/``temp_air``, ``WS10m``/``wind_speed``.
    assumptions:
        :class:`EnergyAssumptions` controlling tilt, azimuth, and losses.

    Returns
    -------
    float
        Annual AC energy per installed kWp in kWh/kWp/yr.
    """
    tilt = assumptions.tilt if assumptions.tilt is not None else abs(lat)
    azimuth = assumptions.azimuth

    weather = _prepare_weather(tmy_df)

    location = pvlib.location.Location(latitude=lat, longitude=lon)
    system = _build_system(tilt=tilt, azimuth=azimuth)

    mc = pvlib.modelchain.ModelChain(
        system,
        location,
        aoi_model="physical",
        spectral_model="no_loss",
    )
    mc.run_model(weather)

    # mc.results.ac is a Series of AC power in W (for a 1 kWp reference system)
    # Summing hourly W values gives Wh; divide by 1000 to get kWh/kWp/yr.
    ac_raw = mc.results.ac
    if ac_raw is None:
        raise RuntimeError("ModelChain produced no AC results; check weather inputs.")
    ac_series: pd.Series = pd.Series(ac_raw)  # type: ignore[arg-type]
    # Replace any negative values (pvlib can return small negatives at night)
    ac_series = ac_series.clip(lower=0.0)
    annual_kwh_per_kwp = float(ac_series.sum()) / 1000.0

    log.debug(
        "specific_yield(lat=%.3f, lon=%.3f, tilt=%.1f, az=%.1f) = %.1f kWh/kWp/yr",
        lat,
        lon,
        tilt,
        azimuth,
        annual_kwh_per_kwp,
    )
    return annual_kwh_per_kwp


def site_energy(
    lat: float,
    lon: float,
    area_km2: float,
    tmy_df: pd.DataFrame,
    assumptions: EnergyAssumptions,
) -> EnergyResult:
    """Compute site-level energy and LCOE for a candidate PV site.

    Parameters
    ----------
    lat:
        Site centroid latitude in decimal degrees.
    lon:
        Site centroid longitude in decimal degrees.
    area_km2:
        Developable land area of the site in km².
    tmy_df:
        Hourly TMY DataFrame (see :func:`specific_yield` for column spec).
    assumptions:
        :class:`EnergyAssumptions` controlling system design and economics.

    Returns
    -------
    EnergyResult
        Named-tuple-style Pydantic model with capacity, yield, and LCOE.

    Notes
    -----
    LCOE formula (after NREL SAM convention)::

        CRF  = r * (1+r)^n / ((1+r)^n - 1)
        LCOE [USD/MWh] = (capex_per_kwp * CRF + opex_per_kwp_yr)
                          / specific_yield_kwh_kwp_yr * 1000

    where r = discount_rate and n = lifetime_yr.
    """
    # Installed capacity
    capacity_mwp = area_km2 * assumptions.packing_density_mwp_per_km2

    # Specific yield via pvlib
    sy = specific_yield(lat, lon, tmy_df, assumptions)

    # Annual generation: sy [kWh/kWp] * capacity [MWp] * 1000 [kWp/MWp] / 1e6 [MWh/kWh]
    # Simplified: sy * capacity_mwp / 1000  (GWh)
    annual_gwh = sy * capacity_mwp / 1000.0

    # LCOE calculation
    r = assumptions.discount_rate
    n = assumptions.lifetime_yr
    crf = r * (1.0 + r) ** n / ((1.0 + r) ** n - 1.0)
    lcoe_usd_per_kwh = (assumptions.capex_per_kwp * crf + assumptions.opex_per_kwp_yr) / sy
    lcoe_usd_per_mwh = lcoe_usd_per_kwh * 1000.0

    log.info(
        "site_energy: lat=%.3f lon=%.3f area=%.1f km² → "
        "capacity=%.1f MWp, sy=%.0f kWh/kWp/yr, "
        "annual=%.2f GWh, LCOE=%.1f USD/MWh",
        lat,
        lon,
        area_km2,
        capacity_mwp,
        sy,
        annual_gwh,
        lcoe_usd_per_mwh,
    )

    return EnergyResult(
        specific_yield_kwh_kwp_yr=sy,
        capacity_mwp=capacity_mwp,
        annual_gwh=annual_gwh,
        lcoe_usd_per_mwh=lcoe_usd_per_mwh,
        assumptions=assumptions,
    )


def site_energy_from_ghi(
    ghi_annual_kwh_per_m2: float,
    area_km2: float,
    assumptions: EnergyAssumptions | None = None,
    performance_ratio: float = 0.75,
) -> EnergyResult:
    """Offline energy estimate from annual GHI — no pvlib/TMY/network.

    Approximates specific yield as ``GHI_annual * performance_ratio`` (PR ≈ 0.75
    for utility-scale fixed-tilt PV). Used by the interactive analysis pipeline so
    every candidate site always carries energy/LCOE fields, computed from the
    already-cached annual-GHI raster (the offline preset runs with zero network).
    The pvlib ModelChain path (:func:`site_energy`) remains the validation-grade
    estimate (within ~2.6% of PVGIS PVcalc); this GHI*PR form trades a few percent
    of accuracy for offline determinism. Reuses the same economics
    (:class:`EnergyAssumptions`) and LCOE/CRF formula as ``site_energy``.
    """
    a = assumptions or EnergyAssumptions()
    sy = max(0.0, float(ghi_annual_kwh_per_m2) * performance_ratio)  # kWh/kWp/yr
    capacity_mwp = area_km2 * a.packing_density_mwp_per_km2
    annual_gwh = sy * capacity_mwp / 1000.0
    r, n = a.discount_rate, a.lifetime_yr
    crf = r * (1.0 + r) ** n / ((1.0 + r) ** n - 1.0)
    lcoe_usd_per_kwh = (a.capex_per_kwp * crf + a.opex_per_kwp_yr) / sy if sy > 0 else float("nan")
    return EnergyResult(
        specific_yield_kwh_kwp_yr=sy,
        capacity_mwp=capacity_mwp,
        annual_gwh=annual_gwh,
        lcoe_usd_per_mwh=lcoe_usd_per_kwh * 1000.0,
        assumptions=a,
    )
