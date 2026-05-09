"""Sprint 8 chunk 8.2 — LiteLLM-style budget tracker."""

from __future__ import annotations

import pytest

from apps.auditor.agents.budget import (
    BudgetExceededError,
    BudgetTracker,
    get_active,
    set_active,
)


def test_add_below_cap_does_not_raise() -> None:
    t = BudgetTracker(cap_usd=0.10)
    t.add(0.04)
    t.add(0.05)
    assert t.spent_usd == pytest.approx(0.09)
    assert t.calls == 2


def test_first_excess_raises_budget_exceeded() -> None:
    t = BudgetTracker(cap_usd=0.05)
    t.add(0.03)
    with pytest.raises(BudgetExceededError) as info:
        t.add(0.04)
    assert info.value.cap_usd == 0.05
    assert info.value.calls == 2


def test_none_cost_uses_surrogate() -> None:
    t = BudgetTracker(cap_usd=10.0, surrogate_usd=0.001)
    t.add(None)
    assert t.spent_usd == pytest.approx(0.001)


def test_negative_cost_clamps_to_surrogate() -> None:
    t = BudgetTracker(cap_usd=10.0, surrogate_usd=0.002)
    t.add(-0.5)
    assert t.spent_usd == pytest.approx(0.002)


def test_active_context_var_round_trip() -> None:
    assert get_active() is None
    t = BudgetTracker(cap_usd=0.5)
    set_active(t)
    try:
        assert get_active() is t
    finally:
        set_active(None)
    assert get_active() is None
