"""Drift detection tools (Sprint 9 chunks 9.2, 9.3).

Pure-function tools the FastMCP server exposes over stdio AND that the
API-side detector imports directly. No network, no orchestration logic
lives here — that belongs in ``apps/api/services/drift.py``.

The diff_snapshots logic mirrors system-design.md 13.2 exactly:

  * normalize evidence by stripping cosmetic keys + sorting key order
  * SHA-256 the canonical-JSON encoding to get a stable projection_hash
  * a drift event fires when the hash changes; the event_type and
    previous/current values come from comparing the dict shapes

Refs: PLAN.md chunks 9.2, 9.3; ADR-0005; system-design 13.2, 13.3, 13.4.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from drift_watcher_mcp.schemas import (
    COSMETIC_KEYS,
    DiffEvent,
    DiffResult,
    DriftEventOut,
    DriftEventType,
    DriftSeverity,
    DriftSnapshot,
    ListDriftEventsResult,
    MarkResolvedResult,
)

# ── normalize + hash ─────────────────────────────────────────────────────────


def normalize_projection(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a cosmetic-stripped, sorted projection of ``raw``.

    Recursive: nested dicts are normalised the same way so a change deep
    inside a JSON tree still surfaces. Lists keep their order — order is
    semantically meaningful for things like protected_branches.

    Sprint 10 will tune the COSMETIC_KEYS set as the eval set surfaces
    false positives.
    """

    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key in sorted(raw.keys()):
        if key in COSMETIC_KEYS:
            continue
        value = raw[key]
        if isinstance(value, dict):
            out[key] = normalize_projection(value)
        elif isinstance(value, list):
            out[key] = _normalize_list(value)
        else:
            out[key] = value
    return out


def _normalize_list(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            out.append(normalize_projection(item))
        elif isinstance(item, list):
            out.append(_normalize_list(item))
        else:
            out.append(item)
    return out


def projection_hash(projection: dict[str, Any] | None) -> str:
    """Stable SHA-256 of the canonical JSON encoding of ``projection``."""

    if not projection:
        return ""
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ── diff_snapshots ───────────────────────────────────────────────────────────


def _classify_event(
    prev: dict[str, Any], current: dict[str, Any]
) -> tuple[DriftEventType, str, DriftSeverity]:
    """Classify a diff and produce a one-liner + severity.

    The control-status diff lives in the API-side detector since it
    needs the readiness state machine; here we only see the projection.
    Therefore this tool emits ``config_changed`` or ``evidence_removed``
    only. ``status_changed`` events are emitted by ``apps/api/services/
    drift.py`` once the orchestrator's `control_map_cache` view of the
    same evidence is overlaid.
    """

    if current == {} and prev != {}:
        return ("evidence_removed", "Evidence row no longer present", "high")
    if prev == {} and current != {}:
        # First sighting — caller should treat as baseline, not drift.
        return ("config_changed", "First snapshot recorded", "low")

    keys_added = sorted(set(current) - set(prev))
    keys_removed = sorted(set(prev) - set(current))
    keys_changed = sorted(
        k for k in current if k in prev and current[k] != prev[k]
    )
    fragments: list[str] = []
    if keys_removed:
        fragments.append(f"removed: {', '.join(keys_removed)}")
    if keys_added:
        fragments.append(f"added: {', '.join(keys_added)}")
    if keys_changed:
        fragments.append(f"changed: {', '.join(keys_changed)}")
    summary = "; ".join(fragments) or "Evidence body changed"
    severity: DriftSeverity = "medium"
    # Heuristic: removing security-relevant keys (e.g. enforcement,
    # require_*) is a high-severity event; everything else is medium.
    high_signal = {
        "enforcement",
        "required_pull_request_reviews",
        "required_status_checks",
        "enforce_admins",
        "required_signatures",
    }
    if any(k in high_signal for k in keys_removed) or any(
        k in high_signal for k in keys_changed
    ):
        severity = "high"
    return ("config_changed", summary, severity)


def diff_snapshots(
    prev_hash: str | None,
    current_hash: str | None,
    *,
    prev_snapshot: DriftSnapshot | None = None,
    current_snapshot: DriftSnapshot | None = None,
) -> DiffResult:
    """Compute drift between two snapshots.

    The MCP tool surface accepts ``prev_hash`` and ``current_hash`` so an
    upstream caller that already has the hashes can short-circuit the
    diff. When the hashes match the result is empty (no drift). When they
    differ, the optional ``prev_snapshot`` / ``current_snapshot`` provide
    the projection bodies; without them, the result reports the hash
    delta but cannot enumerate the changed keys (caller can fetch and
    re-call).
    """

    p_hash = (prev_hash or "").strip()
    c_hash = (current_hash or "").strip()

    if p_hash and c_hash and p_hash == c_hash:
        return DiffResult(prev_hash=p_hash, current_hash=c_hash, drifted=False, events=[])

    # When projections supplied, we can produce structured events.
    if prev_snapshot is not None and current_snapshot is not None:
        prev_proj = prev_snapshot.projection or {}
        curr_proj = current_snapshot.projection or {}
        if prev_proj == curr_proj:
            return DiffResult(
                prev_hash=p_hash or projection_hash(prev_proj),
                current_hash=c_hash or projection_hash(curr_proj),
                drifted=False,
                events=[],
            )
        event_type, summary, severity = _classify_event(prev_proj, curr_proj)
        event = DiffEvent(
            control_id=current_snapshot.control_id or prev_snapshot.control_id,
            event_type=event_type,
            what_changed=summary,
            previous_value=prev_proj,
            current_value=curr_proj,
            severity=severity,
            source_link=current_snapshot.source_link or prev_snapshot.source_link,
        )
        return DiffResult(
            prev_hash=p_hash or projection_hash(prev_proj),
            current_hash=c_hash or projection_hash(curr_proj),
            drifted=True,
            events=[event],
        )

    # Hash-only path: caller knows there's drift but didn't pass bodies.
    return DiffResult(
        prev_hash=p_hash, current_hash=c_hash, drifted=p_hash != c_hash, events=[]
    )


# ── list_drift_events / mark_event_resolved ──────────────────────────────────
#
# The tools below are stdio adapters for the persisted drift_events
# table. The MCP server cannot reach Postgres in tests (no connection
# string in the unit-test process), so we accept an injected fetcher
# callable on the module level. The API-side wiring in
# ``apps/api/services/drift.py`` provides the real fetcher.


_FetcherFn = Callable[[str, str | None], list[DriftEventOut]]
_ResolverFn = Callable[[str, str], bool]


# Stdio transport keeps the server single-process, so the module-level
# singleton is safe. If a future revision serves the tool surface over
# HTTP with concurrent users, swap this for FastMCP's request-scoped
# Context injection so each call carries its own DB handle.
_FETCHER: _FetcherFn | None = None
_RESOLVER: _ResolverFn | None = None


def configure(
    *, fetcher: _FetcherFn | None = None, resolver: _ResolverFn | None = None
) -> None:
    """Inject the DB fetcher / resolver. The API process calls this at
    startup; the stdio MCP server call uses the noop fallback below.
    """

    global _FETCHER, _RESOLVER
    if fetcher is not None:
        _FETCHER = fetcher
    if resolver is not None:
        _RESOLVER = resolver


def reset_for_tests() -> None:
    """Pytest helper: forget any injected callbacks."""

    global _FETCHER, _RESOLVER
    _FETCHER = None
    _RESOLVER = None


def list_drift_events(
    user_id: str, since: str | None = None
) -> ListDriftEventsResult:
    """List recent drift events for ``user_id``.

    ``since`` is an ISO-8601 timestamp; when omitted the fetcher returns
    the most recent N events (the fetcher decides N). Results are
    expected ordered newest-first.
    """

    if _FETCHER is None:
        return ListDriftEventsResult(events=[], count=0)
    events = _FETCHER(user_id, since)
    typed = [
        e if isinstance(e, DriftEventOut) else DriftEventOut.model_validate(e)
        for e in events
    ]
    return ListDriftEventsResult(events=typed, count=len(typed))


def mark_event_resolved(user_id: str, event_id: str) -> MarkResolvedResult:
    """Mark a drift event as resolved.

    The resolver is responsible for enforcing the event_id↔user_id
    pairing (RLS). Returns ok=False if the resolver indicates the event
    does not exist.
    """

    if _RESOLVER is None:
        return MarkResolvedResult(ok=False, event_id=event_id)
    ok = bool(_RESOLVER(user_id, event_id))
    return MarkResolvedResult(ok=ok, event_id=event_id, new_status="resolved")


__all__ = [
    "configure",
    "diff_snapshots",
    "list_drift_events",
    "mark_event_resolved",
    "normalize_projection",
    "projection_hash",
    "reset_for_tests",
]
