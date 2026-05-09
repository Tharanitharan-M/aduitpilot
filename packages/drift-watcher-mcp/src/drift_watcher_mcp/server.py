"""FastMCP server for drift-watcher-mcp (Sprint 9 chunks 9.1-9.3).

Exposes three tools over stdio MCP transport:
  - diff_snapshots(prev, current)       -> DiffResult
  - list_drift_events(user_id, since)   -> ListDriftEventsResult
  - mark_event_resolved(user_id, event) -> MarkResolvedResult

Refs: PLAN.md chunks 9.1, 9.2, 9.3; ADR-0005; system-design 13.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from drift_watcher_mcp.schemas import (
    DiffResult,
    DriftSnapshot,
    ListDriftEventsResult,
    MarkResolvedResult,
)
from drift_watcher_mcp.tools import (
    diff_snapshots as _diff_snapshots,
)
from drift_watcher_mcp.tools import (
    list_drift_events as _list_drift_events,
)
from drift_watcher_mcp.tools import (
    mark_event_resolved as _mark_event_resolved,
)

mcp = FastMCP(
    "drift-watcher-mcp",
    # FastMCP v3 dropped the `description=` kwarg; the second positional
    # argument is `instructions` and serves the same role (the LLM reads
    # it before deciding which tool to call).
    instructions=(
        "Compliance drift detection MCP server for AuditPilot. "
        "Diffs normalized evidence projections, lists persisted drift "
        "events, and resolves them. 2-scan flap protection lives in the "
        "API-side detector that wraps these tools."
    ),
)


@mcp.tool()
async def diff_snapshots(
    prev_hash: str | None = None,
    current_hash: str | None = None,
    prev_snapshot: DriftSnapshot | None = None,
    current_snapshot: DriftSnapshot | None = None,
) -> DiffResult:
    """Compute drift between two evidence snapshots.

    When the projection bodies are supplied, returns structured events
    naming the changed keys. When only the hashes are supplied, reports
    drifted=True if they differ but cannot enumerate keys.
    """

    return await asyncio.to_thread(
        _diff_snapshots,
        prev_hash,
        current_hash,
        prev_snapshot=prev_snapshot,
        current_snapshot=current_snapshot,
    )


@mcp.tool()
async def list_drift_events(
    user_id: str, since: str | None = None
) -> ListDriftEventsResult:
    """Return persisted drift events for the user, newest first.

    The MCP server returns an empty list unless the API process has
    configured a fetcher via :func:`drift_watcher_mcp.tools.configure`.
    """

    return await asyncio.to_thread(_list_drift_events, user_id, since)


@mcp.tool()
async def mark_event_resolved(
    user_id: str, event_id: str
) -> MarkResolvedResult:
    """Mark a drift event resolved. Records who/when in the persisted row."""

    return await asyncio.to_thread(_mark_event_resolved, user_id, event_id)


def main() -> None:
    """CLI entrypoint — run the MCP server over stdio."""
    mcp.run(transport="stdio")


__all__ = ["main", "mcp"]
