"""
LangGraph state schema — AuditPilotState
========================================
Single Pydantic v2 model that travels across every LangGraph node in the
orchestrator graph. Every node reads this state; the orchestrator (and only
the orchestrator) writes to it. AdversarialAuditor returns findings via the
A2A boundary; the orchestrator merges them into `adversarial_findings`
(single-writer invariant from ADR-0002).

The `messages` field uses the LangGraph `add_messages` reducer so every node
append is merged instead of replacing. Other fields use last-writer-wins.

Refs: PLAN.md chunk 2.4; ADR-0001 (LangGraph 1.x runtime);
ADR-0002 (three-agent architecture + single-writer rule);
system-design.md 4 (ERD), 6 (components).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    """A single evidence artifact collected from a user source system.

    Sprint 2 skeleton: the full shape ships in Sprint 5 when `evidence-store-mcp`
    lands. For now it carries enough fields to flow through the orchestrator
    stub and be serialisable by the checkpointer.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: str
    source_type: Literal["github", "clerk", "manual", "mock"] = "mock"
    source_uri: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    collected_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )


class ControlAssessment(BaseModel):
    """One row of the SOC 2 TSC posture grid, grounded in NIST 800-53 controls.

    Sprint 2 skeleton; full shape and caching logic in Sprint 4 chunk 4.5.

    Refs: ADR-0013 (NIST 800-53 catalog, SOC 2 TSC mappings).
    """

    model_config = ConfigDict(extra="forbid")

    tsc_id: str
    status: Literal["passing", "failing", "partial", "unknown"] = "unknown"
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    nist_800_53_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None


class Finding(BaseModel):
    """A single adversarial finding returned by AdversarialAuditor.

    Sprint 2 skeleton; full shape in Sprint 8 when AdversarialAuditor lands.

    Refs: ADR-0002 (three-agent architecture), US-019/US-020.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Literal["low", "medium", "high", "critical"]
    tsc_id: str | None = None
    objection: str
    recommended_next_step: str | None = None


class AuditPilotState(BaseModel):
    """Canonical orchestrator state.

    The Pydantic v2 shape serves three roles:
    1. In-memory value that LangGraph nodes read and write
    2. Checkpointed payload persisted by `AsyncPostgresSaver` (chunk 2.6) — so
       `model_dump()` must round-trip via `model_validate()`
    3. Source of truth for the SSE mapper (chunk 2.7) — the orchestrator
       surfaces typed parts derived from this state

    The model is NOT frozen because LangGraph nodes mutate it in place via
    `add_messages` on the `messages` reducer field. Non-reducer fields use
    last-writer-wins semantics per LangGraph's default merge behaviour.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        # extra="forbid" would reject reducer-produced scratch keys — keep the
        # default "allow" for the state model itself while every *component*
        # model (Evidence, ControlAssessment, Finding) uses extra="forbid".
        extra="ignore",
    )

    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    control_map: dict[str, ControlAssessment] = Field(default_factory=dict)
    adversarial_findings: list[Finding] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    current_step: str = Field(default="init")

    # Optional bookkeeping surfaced in traces and the SSE `finish` payload.
    user_id: str | None = None
    scan_run_id: str | None = None
    thread_id: str | None = None
    intent: str | None = None

    # Sprint 3.5 chunk 3.5.5: the user-chosen repo scope, seeded from
    # connector_scoped_repos at /chat call time. Empty list means the
    # user has not yet picked any repos; the orchestrator refuses to
    # start a readiness scan in that state (ADR-0015 default-deny).
    # Each entry is GitHub's `provider_repo_id` (numeric, string-encoded
    # for parity with the DB column).
    repo_include_list: list[str] = Field(default_factory=list)


# Intents that require a non-empty connector scope before any tool calls.
# Free chat ("free_chat" or None) never requires a scope.
SCOPE_REQUIRED_INTENTS: frozenset[str] = frozenset({"run_readiness_scan"})


class ScanRunValidationError(Exception):
    """Raised when an intent that requires a connector scope is invoked
    with an empty ``repo_include_list``. The /chat SSE bridge catches
    this and emits ``start`` → text → ``finish`` without any tool call
    (Sprint 3.5 chunk 3.5.5)."""


__all__ = [
    "AuditPilotState",
    "ControlAssessment",
    "Evidence",
    "Finding",
    "ScanRunValidationError",
    "SCOPE_REQUIRED_INTENTS",
]
