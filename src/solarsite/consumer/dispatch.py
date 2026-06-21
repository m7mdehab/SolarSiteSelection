"""Self-consumption / export split under a chosen dispatch policy (Phase C).

A real self-consumption fraction needs sub-annual matching of generation to load.
We do NOT have the user's metered load curve, so we offer a few standard residential
LOAD ARCHETYPES (normalised 24-hour shapes) and match the location's REAL average
diurnal generation shape (from the PVGIS TMY ModelChain) against the chosen archetype.
This is explicitly archetype-based, representative-day matching — labelled as such,
never presented as metered.

Three user-selected policy regimes (because the economics differ enormously and
must never be assumed):

* ``net_metering``  — annual banking: self-consumption = min(annual_gen, annual_load);
  any surplus is exported and credited (the grid is a perfect battery).
* ``self_consumption`` — instantaneous matching via the diurnal shapes; surplus is
  exported (feed-in); shortfall is imported.
* ``no_export`` — instantaneous matching, but surplus is curtailed (not exported):
  exported = 0, and the curtailed energy earns nothing.
"""

from __future__ import annotations

__all__ = ["DISPATCH_POLICIES", "LOAD_ARCHETYPES", "self_consumption_split"]


def _normalise(shape: list[float]) -> list[float]:
    total = sum(shape)
    return [x / total for x in shape] if total > 0 else [1.0 / len(shape)] * len(shape)


# Normalised 24-hour residential load archetypes (fraction of daily use per hour,
# midnight..23h). Shapes are standard qualitative profiles (labelled assumptions,
# not metered data); they sum to 1.0 after normalisation.
LOAD_ARCHETYPES: dict[str, list[float]] = {
    # Flat: equal use every hour (a deliberately neutral baseline).
    "flat": _normalise([1.0] * 24),
    # Daytime-heavy: people/appliances active 9-17 (e.g. home worker, daytime cooling).
    "daytime": _normalise(
        [
            0.4,
            0.4,
            0.4,
            0.4,
            0.4,
            0.5,
            0.8,
            1.0,
            1.3,
            1.6,
            1.8,
            1.9,
            2.0,
            1.9,
            1.8,
            1.7,
            1.5,
            1.3,
            1.1,
            1.0,
            0.9,
            0.8,
            0.6,
            0.5,
        ]
    ),
    # Evening-heavy: classic residential peak after work (17-22).
    "evening": _normalise(
        [
            0.5,
            0.4,
            0.4,
            0.4,
            0.4,
            0.5,
            0.8,
            1.1,
            1.0,
            0.8,
            0.7,
            0.7,
            0.7,
            0.7,
            0.7,
            0.8,
            1.1,
            1.6,
            2.1,
            2.3,
            2.1,
            1.6,
            1.1,
            0.7,
        ]
    ),
}

DISPATCH_POLICIES = ("net_metering", "self_consumption", "no_export")


def self_consumption_split(
    annual_gen_kwh: float,
    annual_load_kwh: float,
    *,
    policy: str = "net_metering",
    gen_shape: list[float] | None = None,
    load_profile: str = "evening",
) -> tuple[float, float, float]:
    """Return ``(self_consumed_kwh, exported_kwh, grid_import_kwh)``.

    ``net_metering`` uses the annual balance. ``self_consumption`` / ``no_export``
    match the real diurnal generation shape against the chosen load archetype on a
    representative day, then scale to the year. ``gen_shape`` (24 fractions summing
    to 1) comes from :func:`solarsite.analysis.energy.average_diurnal_profile`; if it
    is absent we fall back to the annual balance (and say so via the policy used).
    """
    gen = max(0.0, float(annual_gen_kwh))
    load = max(0.0, float(annual_load_kwh))

    # Annual net metering (the grid banks surplus) — or any case without a shape.
    if policy == "net_metering" or gen_shape is None or load <= 0:
        self_c = min(gen, load)
        return self_c, gen - self_c, load - self_c

    load_shape = LOAD_ARCHETYPES.get(load_profile, LOAD_ARCHETYPES["evening"])
    daily_gen = gen / 365.0
    daily_load = load / 365.0
    daily_self = sum(min(gen_shape[h] * daily_gen, load_shape[h] * daily_load) for h in range(24))
    self_c = daily_self * 365.0
    surplus = gen - self_c
    exported = 0.0 if policy == "no_export" else surplus
    grid_import = load - self_c
    return self_c, exported, grid_import
