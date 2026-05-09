"""LiteLLM cost tracker for the AdversarialAuditor (Sprint 8 chunk 8.2).

The orchestrator imposes a per-task USD cap on every adversarial run.
We can't trust the LLM to respect this — only the harness can. This
module wires LiteLLM's success/failure callback so every chunk of cost
LiteLLM observes is summed against the cap. The first chunk that pushes
the running total past the cap raises ``BudgetExceededError`` and the
service ends the run with status ``TASK_STATE_BUDGET_EXCEEDED``.

LiteLLM passes a ``response_cost`` field on its ``kwargs`` dict in the
async-success callback. When the underlying model does not report cost
(e.g. local stubs in tests), we fall back to a per-call surrogate so the
loop still terminates.

Refs: PLAN.md Sprint 8 chunk 8.2; ADR-0002 (cost cap).
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Stub surrogate cost per call when LiteLLM does not provide one.
DEFAULT_SURROGATE_USD: float = 0.001


class BudgetExceededError(Exception):
    """Raised inside the agent loop when the running cost exceeds the cap."""

    def __init__(self, *, spent_usd: float, cap_usd: float, calls: int) -> None:
        super().__init__(
            f"AdversarialAuditor budget exceeded: spent ${spent_usd:.4f} / "
            f"cap ${cap_usd:.4f} after {calls} call(s)"
        )
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd
        self.calls = calls


@dataclass
class BudgetTracker:
    """Per-run accumulator. Thread-safe; instances are not reusable."""

    cap_usd: float
    surrogate_usd: float = DEFAULT_SURROGATE_USD
    spent_usd: float = 0.0
    calls: int = 0
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def add(self, usd: float | None) -> None:
        """Record one LLM call's cost; raise if the cap is exceeded."""
        amount = float(usd) if usd is not None else self.surrogate_usd
        if amount < 0:
            amount = self.surrogate_usd
        assert self._lock is not None
        with self._lock:
            self.spent_usd += amount
            self.calls += 1
            if self.spent_usd > self.cap_usd:
                raise BudgetExceededError(
                    spent_usd=self.spent_usd,
                    cap_usd=self.cap_usd,
                    calls=self.calls,
                )

    def snapshot(self) -> dict[str, float | int]:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "cap_usd": self.cap_usd,
            "calls": self.calls,
        }


_active: ContextVar[BudgetTracker | None] = ContextVar("active_budget", default=None)


def set_active(tracker: BudgetTracker | None) -> None:
    _active.set(tracker)


def get_active() -> BudgetTracker | None:
    return _active.get()


def litellm_success_callback(  # noqa: ANN401, ARG001
    kwargs: dict[str, Any],
    completion_response: Any,
    start_time: Any,
    end_time: Any,
) -> None:
    """LiteLLM ``success_callback`` hook.

    LiteLLM invokes this synchronously after every successful call. If
    no tracker is bound to the current context, this is a no-op (e.g.
    the orchestrator path that doesn't enforce a per-call cap).
    """

    tracker = get_active()
    if tracker is None:
        return
    cost = kwargs.get("response_cost")
    try:
        tracker.add(cost)
    except BudgetExceededError as exc:
        logger.warning("auditor.budget.exceeded %s", exc)
        # Re-raise so LiteLLM aborts the in-flight call instead of
        # returning a (paid) completion the caller would not honour.
        raise


__all__ = [
    "BudgetExceededError",
    "BudgetTracker",
    "DEFAULT_SURROGATE_USD",
    "get_active",
    "litellm_success_callback",
    "set_active",
]
