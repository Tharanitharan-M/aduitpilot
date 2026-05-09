"""Pydantic v2 schemas for drift-watcher-mcp (Sprint 9 chunks 9.1-9.3).

Every schema sets ``extra="forbid"`` so ``model_json_schema()`` produces
``additionalProperties: false`` — the mcp-server-validator requirement.

Refs: PLAN.md chunks 9.1, 9.2, 9.3; ADR-0005; system-design.md 13.1-13.4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DriftEventType = Literal["status_changed", "config_changed", "evidence_removed"]
DriftSeverity = Literal["low", "medium", "high"]
DriftStatus = Literal["open", "resolved", "dismissed"]

# Cosmetic JSON keys that drift detection always strips before hashing.
# system-design 13.2 enumerates the canonical exclusion set; keeping it
# in one place keeps the `diff_snapshots` tool and the API-side detector
# in sync.
COSMETIC_KEYS: frozenset[str] = frozenset(
    {
        "fetched_at",
        "etag",
        "_links",
        "node_id",
        "url",
        "html_url",
        "api_url",
        "links",
        "updated_at",
        "_meta",
    }
)


class DriftSnapshot(BaseModel):
    """One side of a snapshot diff: the evidence projection at a point in time."""

    model_config = ConfigDict(extra="forbid")

    control_id: str = Field(min_length=1, description="SOC 2 TSC clause id, e.g. 'CC6.1'.")
    projection: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Normalized projection of evidence.raw — cosmetic keys stripped, "
            "remaining keys sorted. Hashed to drive drift detection."
        ),
    )
    projection_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the canonical JSON encoding of `projection`. "
            "Empty string when the snapshot has no evidence."
        ),
    )
    source_link: str | None = Field(
        default=None,
        description="Deep link back to the source-system setting (e.g. GitHub repo settings page).",
    )
    captured_at: datetime | None = Field(
        default=None,
        description="When the snapshot evidence was collected.",
    )


class DiffEvent(BaseModel):
    """One drift event surfaced by `diff_snapshots`."""

    model_config = ConfigDict(extra="forbid")

    control_id: str = Field(min_length=1)
    event_type: DriftEventType
    what_changed: str = Field(
        default="",
        description="Human-readable one-liner describing the change.",
    )
    previous_value: dict[str, Any] = Field(default_factory=dict)
    current_value: dict[str, Any] = Field(default_factory=dict)
    severity: DriftSeverity = "medium"
    source_link: str | None = None


class DiffResult(BaseModel):
    """Return shape for `diff_snapshots`."""

    model_config = ConfigDict(extra="forbid")

    prev_hash: str = Field(default="", description="Hash of the previous projection.")
    current_hash: str = Field(default="", description="Hash of the current projection.")
    drifted: bool = Field(
        default=False,
        description="True when prev_hash != current_hash and the change is not cosmetic.",
    )
    events: list[DiffEvent] = Field(default_factory=list)


class DriftEventOut(BaseModel):
    """A persisted drift event row, returned by `list_drift_events`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="UUID of the drift_events row.")
    user_id: str
    control_id: str
    event_type: DriftEventType
    what_changed: str = ""
    previous_value: dict[str, Any] = Field(default_factory=dict)
    current_value: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str = ""
    source_link: str | None = None
    severity: DriftSeverity = "medium"
    detected_at: datetime
    status: DriftStatus = "open"
    content_hash: str = Field(
        default="",
        description=(
            "SHA-256 of the canonical (event_type, control_id, current projection) "
            "tuple. Used to suppress re-fire on dismissed events."
        ),
    )


class ListDriftEventsResult(BaseModel):
    """Return shape for `list_drift_events`."""

    model_config = ConfigDict(extra="forbid")

    events: list[DriftEventOut] = Field(default_factory=list)
    count: int = 0


class MarkResolvedResult(BaseModel):
    """Return shape for `mark_event_resolved`."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    event_id: str
    new_status: DriftStatus = "resolved"


__all__ = [
    "COSMETIC_KEYS",
    "DiffEvent",
    "DiffResult",
    "DriftEventOut",
    "DriftEventType",
    "DriftSeverity",
    "DriftSnapshot",
    "DriftStatus",
    "ListDriftEventsResult",
    "MarkResolvedResult",
]
