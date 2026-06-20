"""Itemized PV loss stack (E2) — replaces the single opaque ``0.96`` derate.

Why
---
The earlier engine encoded *all* system losses in one number
(``eta_inv_nom = 0.96``), conflating inverter efficiency with DC-side losses
(soiling, mismatch, wiring, …). A real fixed-tilt utility system loses far more
than 4 % once every mechanism is counted (~14-20 % combined). A single magic
number is neither auditable nor honest, so this module makes the derate a
transparent, line-by-line breakdown.

Model
-----
The 10 DC-side components are the **NREL PVWatts** named-loss set. They combine
**multiplicatively**, exactly as PVWatts V5 (Dobos 2014, NREL/TP-6A20-62641)
and :func:`pvlib.pvsystem.pvwatts_losses`::

    dc_loss_fraction = 1 - Π_i (1 - L_i / 100)

With the documented PVWatts defaults this equals **14.0757 %** — verified
against ``pvlib.pvsystem.pvwatts_losses()`` in :mod:`tests.test_losses` (an
independent in-library oracle that catches any transcription error).

Inverter **nominal efficiency** is modelled *separately* (PVWatts treats it
apart from the 14 % DC system losses), default **0.96**, applied inside the
pvlib inverter model. The combined end-to-end derate surfaced to users is::

    total_derate = (1 - dc_loss_fraction) * inverter_nominal_efficiency

Each component is a documented PVWatts default; none is a project-invented
figure. See the ``LossComponent`` table below for the per-item source note.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

__all__ = ["DC_LOSS_FIELDS", "LossLineItem", "LossStack"]

#: Ordered DC-side PVWatts loss-component field names (the 10 that combine
#: multiplicatively into ``dc_loss_fraction``). Order is the PVWatts manual order.
DC_LOSS_FIELDS: tuple[str, ...] = (
    "soiling",
    "shading",
    "snow",
    "mismatch",
    "wiring",
    "connections",
    "lid",
    "nameplate_rating",
    "age",
    "availability",
)


class LossLineItem(BaseModel):
    """One row of the surfaced loss breakdown (name, percent, kind)."""

    name: str
    percent: float = Field(..., description="Loss for this line item, in percent.")
    kind: str = Field(..., description="'dc' (DC-side system loss) or 'inverter'.")


class LossStack(BaseModel):
    """Itemized PV system losses — PVWatts named components (DC) + inverter.

    All ten DC component defaults are the **NREL PVWatts** defaults (verified to
    reproduce ``pvlib.pvsystem.pvwatts_losses()`` = 14.0757 %). Units are
    *percent* (e.g. ``soiling=2.0`` means 2 %). They combine multiplicatively,
    never additively, so the order of components does not change the result.
    """

    # --- DC-side PVWatts loss components (percent) ----------------------------
    soiling: float = Field(default=2.0, ge=0.0, le=100.0, description="Dust/dirt on modules.")
    shading: float = Field(
        default=3.0, ge=0.0, le=100.0, description="Near/far shading (flat PVWatts approx)."
    )
    snow: float = Field(
        default=0.0, ge=0.0, le=100.0, description="Snow cover (0 in snow-free climates)."
    )
    mismatch: float = Field(
        default=2.0, ge=0.0, le=100.0, description="Module-to-module I-V mismatch."
    )
    wiring: float = Field(
        default=2.0, ge=0.0, le=100.0, description="DC + AC resistive wiring loss."
    )
    connections: float = Field(
        default=0.5, ge=0.0, le=100.0, description="Connector resistive loss."
    )
    lid: float = Field(
        default=1.5, ge=0.0, le=100.0, description="Light-induced degradation (first-year)."
    )
    nameplate_rating: float = Field(
        default=1.0, ge=0.0, le=100.0, description="Nameplate tolerance vs STC."
    )
    age: float = Field(
        default=0.0, ge=0.0, le=100.0, description="Long-term degradation (0 at year 0)."
    )
    availability: float = Field(
        default=3.0, ge=0.0, le=100.0, description="Downtime / grid unavailability."
    )

    # --- Inverter (modelled separately, PVWatts convention) -------------------
    inverter_nominal_efficiency: float = Field(
        default=0.96,
        gt=0.0,
        le=1.0,
        description="Nominal inverter efficiency (fraction). PVWatts default 0.96.",
    )

    @field_validator("inverter_nominal_efficiency")
    @classmethod
    def _eff_reasonable(cls, v: float) -> float:
        if not (0.80 <= v <= 1.0):
            raise ValueError(f"inverter_nominal_efficiency={v} outside plausible [0.80, 1.0]")
        return v

    # ------------------------------------------------------------------ derates
    def dc_components(self) -> dict[str, float]:
        """Ordered mapping of the 10 DC component names → percent."""
        return {f: float(getattr(self, f)) for f in DC_LOSS_FIELDS}

    @property
    def dc_loss_fraction(self) -> float:
        """Combined DC-side loss as a fraction in [0, 1].

        ``1 - Π_i (1 - L_i/100)`` — the exact PVWatts / pvlib multiplicative
        combination of the ten DC components.
        """
        prod = 1.0
        for pct in self.dc_components().values():
            prod *= 1.0 - pct / 100.0
        return 1.0 - prod

    @property
    def dc_derate(self) -> float:
        """DC-side keep-fraction, ``1 - dc_loss_fraction`` (in [0, 1])."""
        return 1.0 - self.dc_loss_fraction

    @property
    def total_derate(self) -> float:
        """End-to-end keep-fraction: DC derate x inverter nominal efficiency."""
        return self.dc_derate * float(self.inverter_nominal_efficiency)

    @property
    def total_loss_fraction(self) -> float:
        """End-to-end loss fraction, ``1 - total_derate`` (in [0, 1])."""
        return 1.0 - self.total_derate

    def line_items(self) -> list[LossLineItem]:
        """Surfaced breakdown: one row per DC component + the inverter row."""
        rows = [
            LossLineItem(name=f, percent=float(getattr(self, f)), kind="dc") for f in DC_LOSS_FIELDS
        ]
        rows.append(
            LossLineItem(
                name="inverter",
                percent=round(100.0 * (1.0 - float(self.inverter_nominal_efficiency)), 4),
                kind="inverter",
            )
        )
        return rows
