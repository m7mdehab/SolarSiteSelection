"""Tests for the Phase C dispatch / self-consumption split."""

from __future__ import annotations

import pytest

from solarsite.consumer.dispatch import (
    DISPATCH_POLICIES,
    LOAD_ARCHETYPES,
    self_consumption_split,
)

# A peaky midday generation shape (all energy 10:00-14:00) — worst case for an
# evening-heavy household, best case for net metering.
_MIDDAY = [0.0] * 24
for _h in (10, 11, 12, 13):
    _MIDDAY[_h] = 0.25


def test_archetypes_normalised() -> None:
    for name, shape in LOAD_ARCHETYPES.items():
        assert len(shape) == 24
        assert sum(shape) == pytest.approx(1.0), name


def test_net_metering_is_annual_balance() -> None:
    self_c, exp, imp = self_consumption_split(10000.0, 6000.0, policy="net_metering")
    assert self_c == pytest.approx(6000.0)  # min(gen, load)
    assert exp == pytest.approx(4000.0)
    assert imp == pytest.approx(0.0)


def test_self_consumption_diurnal_below_net_metering() -> None:
    """Midday generation vs evening load → diurnal self-consumption < the annual cap."""
    nm, _, _ = self_consumption_split(8000.0, 8000.0, policy="net_metering")
    sc, exp, _imp = self_consumption_split(
        8000.0, 8000.0, policy="self_consumption", gen_shape=_MIDDAY, load_profile="evening"
    )
    assert sc < nm  # the diurnal mismatch means you can't self-consume it all
    assert exp > 0 and _imp > 0  # you export midday surplus and import in the evening


def test_no_export_curtails_surplus() -> None:
    sc, exp, _imp = self_consumption_split(
        8000.0, 8000.0, policy="no_export", gen_shape=_MIDDAY, load_profile="evening"
    )
    assert exp == pytest.approx(0.0)  # surplus is curtailed, not exported
    assert sc > 0


def test_unknown_policy_or_no_shape_falls_back_to_annual() -> None:
    # No gen_shape -> annual balance even under a diurnal policy (honest fallback).
    sc, _exp, _imp = self_consumption_split(10000.0, 6000.0, policy="self_consumption")
    assert sc == pytest.approx(6000.0)


def test_policies_constant() -> None:
    assert set(DISPATCH_POLICIES) == {"net_metering", "self_consumption", "no_export"}
