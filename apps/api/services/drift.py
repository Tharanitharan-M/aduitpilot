"""Drift detector + 2-scan flap protection (Sprint 9 chunk 9.4).

Workflow per scan run:

  1. For each monitored (user, control_id) the orchestrator already has a
     freshly-computed projection (a normalized view of the evidence
     anchoring that control).
  2. We look up the ``drift_snapshots`` row for that pair.
  3. If the new hash equals ``confirmed_hash`` -> no drift, refresh
     ``last_seen_at`` and clear any pending hash.
  4. If the new hash differs from ``confirmed_hash``:
       a. If it equals the existing ``pending_hash`` -> the change has been
          observed twice in a row. Promote pending -> confirmed AND emit a
          drift_events row (subject to re-fire suppression by content_hash).
       b. Otherwise -> first sighting of this new hash. Park it in
          ``pending_hash`` and DO NOT emit. (system-design 13.3.)
  5. First-ever scan (``confirmed_hash == ''``) is baseline only — never
     emits drift.

Refs: PLAN.md chunks 9.4, 9.5, 9.9; ADR-0005, ADR-0008; system-design 13.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from drift_watcher_mcp.schemas import (
    COSMETIC_KEYS,  # noqa: F401  (re-export — keeps the projection shape canonical)
    DiffEvent,
    DriftEventOut,
    DriftEventType,
    DriftSeverity,
)
from drift_watcher_mcp.tools import (
    diff_snapshots,
    normalize_projection,
    projection_hash,
)
from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


# ── Snapshot input + result types ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    """One control's projection for a single scan tick.

    Built by the orchestrator (or, for cron, by ``apps/api/services/
    drift_worker.py``) from the latest evidence + control_map_cache row.
    """

    control_id: str
    projection: dict[str, Any]
    source_link: str | None = None
    suggested_fix: str | None = None
    status_label: str | None = None  # e.g. "passing"/"failing" — informs the event message


@dataclass(slots=True)
class DriftDetectionOutcome:
    """The detector's per-call summary, for tracing / tests."""

    user_id: str
    snapshots_seen: int = 0
    pending_promoted: int = 0
    events_emitted: int = 0
    events_suppressed_by_optout: int = 0
    events_suppressed_by_content_hash: int = 0
    fired_event_ids: list[str] = field(default_factory=list)


# ── Hashing helper ──────────────────────────────────────────────────────────


def compute_event_content_hash(
    *, control_id: str, event_type: DriftEventType, current_value: dict[str, Any]
) -> str:
    """SHA-256 over (control_id, event_type, normalised current_value).

    The detector uses this to decide whether a previously-dismissed event
    should re-fire: only when the underlying configuration produces a new
    content_hash do we re-emit. (US-026.)
    """

    canonical = json.dumps(
        {
            "control_id": control_id,
            "event_type": event_type,
            "current": normalize_projection(current_value),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── DB-IO helpers (small, focused so the worker can stub them in tests) ─────


async def _set_user_scope(conn: Any, user_id: str) -> None:
    await conn.execute("SELECT set_config('app.current_user_id', %s, true)", (user_id,))


async def _load_monitored_optout(
    pool: AsyncConnectionPool, *, user_id: str
) -> set[str]:
    """Return the set of control_ids the user has explicitly opted OUT of.

    Default-on: any control NOT in this set is monitored. Mirrors
    system-design 13.6.
    """

    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT control_id
                    FROM monitored_controls
                    WHERE user_id = %s AND monitored = false
                    """,
                    (user_id,),
                )
                rows = await cur.fetchall()
    return {r[0] for r in rows}


async def _load_snapshot_rows(
    pool: AsyncConnectionPool, *, user_id: str, control_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not control_ids:
        return {}
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT control_id, confirmed_hash, confirmed_value,
                           pending_hash, pending_value, source_link,
                           pending_seen_at
                    FROM drift_snapshots
                    WHERE user_id = %s AND control_id = ANY(%s)
                    """,
                    (user_id, control_ids),
                )
                rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r[0]] = {
            "confirmed_hash": r[1] or "",
            "confirmed_value": r[2] or {},
            "pending_hash": r[3] or "",
            "pending_value": r[4] or {},
            "source_link": r[5],
            "pending_seen_at": r[6],
        }
    return out


async def _upsert_snapshot(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    control_id: str,
    confirmed_hash: str,
    confirmed_value: dict[str, Any],
    pending_hash: str,
    pending_value: dict[str, Any],
    source_link: str | None,
) -> None:
    with tracer.start_as_current_span("db.drift_snapshots.upsert") as span:
        span.set_attribute("db.table", "drift_snapshots")
        span.set_attribute("drift.control_id", control_id)
        async with pool.connection() as conn:
            async with conn.transaction():
                await _set_user_scope(conn, user_id)
                await conn.execute(
                    """
                    INSERT INTO drift_snapshots
                        (user_id, control_id, confirmed_hash, confirmed_value,
                         pending_hash, pending_value, pending_seen_at,
                         source_link, last_seen_at, updated_at)
                    VALUES
                        (%s, %s, %s, %s::jsonb, %s, %s::jsonb,
                         CASE WHEN %s = '' THEN NULL ELSE now() END,
                         %s, now(), now())
                    ON CONFLICT (user_id, control_id) DO UPDATE
                       SET confirmed_hash  = EXCLUDED.confirmed_hash,
                           confirmed_value = EXCLUDED.confirmed_value,
                           pending_hash    = EXCLUDED.pending_hash,
                           pending_value   = EXCLUDED.pending_value,
                           -- database-reviewer HIGH-3: preserve the
                           -- original first-sighting timestamp when
                           -- the same pending hash is re-observed
                           -- (the flap protector needs an accurate
                           -- "how long has this been pending" signal
                           -- for future telemetry).
                           pending_seen_at = CASE
                               WHEN EXCLUDED.pending_hash = '' THEN NULL
                               WHEN EXCLUDED.pending_hash = drift_snapshots.pending_hash
                                    THEN drift_snapshots.pending_seen_at
                               ELSE now()
                           END,
                           source_link     = EXCLUDED.source_link,
                           last_seen_at    = now(),
                           updated_at      = now()
                    """,
                    (
                        user_id,
                        control_id,
                        confirmed_hash,
                        json.dumps(confirmed_value),
                        pending_hash,
                        json.dumps(pending_value),
                        pending_hash,
                        source_link,
                    ),
                )


async def _existing_event_for_content_hash(
    pool: AsyncConnectionPool, *, user_id: str, content_hash: str
) -> str | None:
    """Return the existing drift_events.id (open OR dismissed) sharing this hash, else None.

    Used by the re-fire suppressor: if the user dismissed an event for
    this content_hash, we won't insert a new row until the hash changes.
    """

    if not content_hash:
        return None
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id::text FROM drift_events
                    WHERE user_id = %s
                      AND content_hash = %s
                      AND status IN ('open', 'dismissed')
                    ORDER BY detected_at DESC LIMIT 1
                    """,
                    (user_id, content_hash),
                )
                row = await cur.fetchone()
    return row[0] if row else None


async def _insert_drift_event(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    control_id: str,
    event_type: DriftEventType,
    what_changed: str,
    previous_value: dict[str, Any],
    current_value: dict[str, Any],
    suggested_fix: str,
    source_link: str | None,
    severity: DriftSeverity,
    content_hash: str,
) -> str:
    event_id = str(uuid.uuid4())
    with tracer.start_as_current_span("db.drift_events.insert") as span:
        span.set_attribute("db.table", "drift_events")
        span.set_attribute("drift.control_id", control_id)
        span.set_attribute("drift.event_type", event_type)
        async with pool.connection() as conn:
            async with conn.transaction():
                await _set_user_scope(conn, user_id)
                await conn.execute(
                    """
                    INSERT INTO drift_events
                        (id, user_id, control_id, event_type, what_changed,
                         previous_value, current_value, suggested_fix,
                         source_link, severity, status, content_hash,
                         detected_at, updated_at)
                    VALUES
                        (%s::uuid, %s, %s, %s, %s,
                         %s::jsonb, %s::jsonb, %s,
                         %s, %s, 'open', %s,
                         now(), now())
                    """,
                    (
                        event_id,
                        user_id,
                        control_id,
                        event_type,
                        (what_changed or "")[:2000],
                        json.dumps(previous_value),
                        json.dumps(current_value),
                        (suggested_fix or "")[:2000],
                        source_link,
                        severity,
                        content_hash,
                    ),
                )
    return event_id


# ── Detector entry point ─────────────────────────────────────────────────────


async def detect_drift(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    snapshots: Iterable[ControlSnapshot],
) -> DriftDetectionOutcome:
    """Run drift detection for one user across an iterable of fresh snapshots.

    The caller (cron worker for the 6-hour schedule, or the orchestrator
    for the on-demand path) supplies pre-computed snapshots — this
    function handles the bookkeeping + flap protection + persistence.

    Wrapped in an OTel span so each detector run produces one root span
    per (user, scan tick) with attributes summarising the outcome —
    the worker-level ``drift.scan`` span sits above this and the DB
    helper spans sit below.
    """

    span_cm = tracer.start_as_current_span("drift.detect")
    with span_cm as span:
        span.set_attribute("user.id", user_id)
        return await _detect_drift_inner(pool, user_id=user_id, snapshots=snapshots, span=span)


async def _detect_drift_inner(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    snapshots: Iterable[ControlSnapshot],
    span: Any,
) -> DriftDetectionOutcome:
    outcome = DriftDetectionOutcome(user_id=user_id)
    snapshots = list(snapshots)
    outcome.snapshots_seen = len(snapshots)
    span.set_attribute("drift.snapshots_seen", outcome.snapshots_seen)
    if not snapshots:
        return outcome

    optout = await _load_monitored_optout(pool, user_id=user_id)
    rows = await _load_snapshot_rows(
        pool, user_id=user_id, control_ids=[s.control_id for s in snapshots]
    )

    for snap in snapshots:
        if snap.control_id in optout:
            outcome.events_suppressed_by_optout += 1
            continue
        new_proj = normalize_projection(snap.projection)
        new_hash = projection_hash(new_proj)
        prior = rows.get(snap.control_id, {})
        confirmed_hash = prior.get("confirmed_hash") or ""
        confirmed_value = prior.get("confirmed_value") or {}
        pending_hash_prev = prior.get("pending_hash") or ""

        if new_hash == confirmed_hash:
            # No change at all. Clear any stale pending value.
            await _upsert_snapshot(
                pool,
                user_id=user_id,
                control_id=snap.control_id,
                confirmed_hash=confirmed_hash,
                confirmed_value=confirmed_value,
                pending_hash="",
                pending_value={},
                source_link=snap.source_link or prior.get("source_link"),
            )
            continue

        # Change detected. Apply 2-scan flap protection.
        if confirmed_hash == "":
            # First time we ever see this control — baseline only.
            await _upsert_snapshot(
                pool,
                user_id=user_id,
                control_id=snap.control_id,
                confirmed_hash=new_hash,
                confirmed_value=new_proj,
                pending_hash="",
                pending_value={},
                source_link=snap.source_link,
            )
            continue

        if new_hash != pending_hash_prev:
            # First sighting of this drift — park as pending. Don't emit.
            await _upsert_snapshot(
                pool,
                user_id=user_id,
                control_id=snap.control_id,
                confirmed_hash=confirmed_hash,
                confirmed_value=confirmed_value,
                pending_hash=new_hash,
                pending_value=new_proj,
                source_link=snap.source_link or prior.get("source_link"),
            )
            continue

        # Second consecutive sighting -> emit drift.
        prior_link = prior.get("source_link")
        prev_snap = _snapshot_for_diff(snap.control_id, confirmed_value, prior_link)
        curr_snap = _snapshot_for_diff(snap.control_id, new_proj, snap.source_link)
        diff = diff_snapshots(
            None,
            None,
            prev_snapshot=prev_snap,
            current_snapshot=curr_snap,
        )
        event_type, what_changed, severity = _resolve_classification(diff)
        content_hash = compute_event_content_hash(
            control_id=snap.control_id,
            event_type=event_type,
            current_value=new_proj,
        )

        # Re-fire suppression: only insert if no existing open/dismissed
        # event has the same content_hash.
        existing = await _existing_event_for_content_hash(
            pool, user_id=user_id, content_hash=content_hash
        )
        if existing is None:
            event_id = await _insert_drift_event(
                pool,
                user_id=user_id,
                control_id=snap.control_id,
                event_type=event_type,
                what_changed=what_changed,
                previous_value=confirmed_value,
                current_value=new_proj,
                suggested_fix=snap.suggested_fix or "",
                source_link=snap.source_link or prior.get("source_link"),
                severity=severity,
                content_hash=content_hash,
            )
            outcome.events_emitted += 1
            outcome.fired_event_ids.append(event_id)
        else:
            outcome.events_suppressed_by_content_hash += 1

        # Promote pending -> confirmed regardless of whether we emitted
        # a row, so the next scan starts from the new baseline.
        await _upsert_snapshot(
            pool,
            user_id=user_id,
            control_id=snap.control_id,
            confirmed_hash=new_hash,
            confirmed_value=new_proj,
            pending_hash="",
            pending_value={},
            source_link=snap.source_link,
        )
        outcome.pending_promoted += 1

    span.set_attribute("drift.events_emitted", outcome.events_emitted)
    span.set_attribute("drift.pending_promoted", outcome.pending_promoted)
    span.set_attribute(
        "drift.events_suppressed_by_optout", outcome.events_suppressed_by_optout
    )
    span.set_attribute(
        "drift.events_suppressed_by_content_hash",
        outcome.events_suppressed_by_content_hash,
    )
    return outcome


def _snapshot_for_diff(
    control_id: str, projection: dict[str, Any], source_link: str | None
):
    from drift_watcher_mcp.schemas import DriftSnapshot

    return DriftSnapshot(
        control_id=control_id,
        projection=projection,
        projection_hash=projection_hash(projection),
        source_link=source_link,
    )


def _resolve_classification(diff_result):
    if not diff_result.events:
        return ("config_changed", "Configuration changed", "medium")
    ev: DiffEvent = diff_result.events[0]
    return (ev.event_type, ev.what_changed, ev.severity)


# ── Read helpers used by the API routes ──────────────────────────────────────


async def list_drift_events_for_user(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    status: str | None = None,
    limit: int = 200,
) -> list[DriftEventOut]:
    sql = (
        "SELECT id::text, user_id, control_id, event_type, what_changed, "
        "previous_value, current_value, suggested_fix, source_link, severity, "
        "detected_at, status, content_hash "
        "FROM drift_events WHERE user_id = %s"
    )
    params: tuple = (user_id,)
    if status is not None:
        sql += " AND status = %s"
        params = (*params, status)
    sql += " ORDER BY detected_at DESC LIMIT %s"
    params = (*params, limit)

    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
    return [_row_to_event_out(r) for r in rows]


def _row_to_event_out(row: tuple) -> DriftEventOut:
    (
        id_,
        user_id,
        control_id,
        event_type,
        what_changed,
        previous_value,
        current_value,
        suggested_fix,
        source_link,
        severity,
        detected_at,
        status_,
        content_hash,
    ) = row
    if isinstance(detected_at, str):
        detected = datetime.fromisoformat(detected_at)
    elif isinstance(detected_at, datetime):
        detected = detected_at if detected_at.tzinfo else detected_at.replace(tzinfo=UTC)
    else:
        detected = datetime.now(UTC)
    return DriftEventOut(
        id=id_,
        user_id=user_id,
        control_id=control_id,
        event_type=event_type,
        what_changed=what_changed or "",
        previous_value=previous_value or {},
        current_value=current_value or {},
        suggested_fix=suggested_fix or "",
        source_link=source_link,
        severity=severity,
        detected_at=detected,
        status=status_,
        content_hash=content_hash or "",
    )


async def dismiss_drift_event(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    event_id: str,
    reason: str,
) -> DriftEventOut | None:
    """Mark an event dismissed. Re-fire suppression keys on content_hash."""

    with tracer.start_as_current_span("db.drift_events.dismiss") as span:
        span.set_attribute("db.table", "drift_events")
        async with pool.connection() as conn:
            async with conn.transaction():
                await _set_user_scope(conn, user_id)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE drift_events
                        SET status = 'dismissed',
                            dismissed_reason = %s,
                            updated_at = now()
                        WHERE user_id = %s AND id = %s::uuid
                          AND status = 'open'
                        RETURNING id::text, user_id, control_id, event_type,
                                  what_changed, previous_value, current_value,
                                  suggested_fix, source_link, severity,
                                  detected_at, status, content_hash
                        """,
                        ((reason or "")[:2000], user_id, event_id),
                    )
                    row = await cur.fetchone()
                    span.set_attribute("drift.dismiss.matched", row is not None)
                    return _row_to_event_out(row) if row else None


async def resolve_drift_event(
    pool: AsyncConnectionPool, *, user_id: str, event_id: str
) -> DriftEventOut | None:
    with tracer.start_as_current_span("db.drift_events.resolve"):
        async with pool.connection() as conn:
            async with conn.transaction():
                await _set_user_scope(conn, user_id)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE drift_events
                        SET status = 'resolved', updated_at = now()
                        WHERE user_id = %s AND id = %s::uuid
                          AND status = 'open'
                        RETURNING id::text, user_id, control_id, event_type,
                                  what_changed, previous_value, current_value,
                                  suggested_fix, source_link, severity,
                                  detected_at, status, content_hash
                        """,
                        (user_id, event_id),
                    )
                    row = await cur.fetchone()
                    return _row_to_event_out(row) if row else None


async def list_monitored_optouts(
    pool: AsyncConnectionPool, *, user_id: str
) -> set[str]:
    return await _load_monitored_optout(pool, user_id=user_id)


@dataclass(slots=True)
class DriftStatus:
    """Heartbeat snapshot of the watcher's state for one user.

    Surfaced via GET /api/drift/status so the FE can render a "Last
    drift scan: 2 min ago — scanned 76 controls, 0 drift events" line
    confirming the worker actually ran. Without this, a baseline-only
    first scan looks identical to a stuck job to the user.
    """

    baselines: int = 0
    last_scan_at: datetime | None = None
    events_total: int = 0
    events_open: int = 0


async def get_drift_status(
    pool: AsyncConnectionPool, *, user_id: str
) -> DriftStatus:
    """Aggregate baseline count + last-seen timestamp + event totals.

    Three small queries instead of a single CTE so the queries stay
    boring and each can use the existing indexes:
      - drift_snapshots: ix_drift_snapshots__user
      - drift_events:    ix_drift_events__user_status_detected
    """

    out = DriftStatus()
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT count(*)::int, max(last_seen_at)
                    FROM drift_snapshots
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
                if row is not None:
                    out.baselines = int(row[0] or 0)
                    out.last_scan_at = row[1]

                await cur.execute(
                    """
                    SELECT
                        count(*)::int                              AS total,
                        count(*) FILTER (WHERE status = 'open')::int AS open_count
                    FROM drift_events
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
                if row is not None:
                    out.events_total = int(row[0] or 0)
                    out.events_open = int(row[1] or 0)
    return out


async def set_monitored(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    control_id: str,
    monitored: bool,
) -> None:
    with tracer.start_as_current_span("db.monitored_controls.upsert") as span:
        span.set_attribute("db.table", "monitored_controls")
        span.set_attribute("drift.control_id", control_id)
        span.set_attribute("drift.monitored", monitored)
        async with pool.connection() as conn:
            async with conn.transaction():
                await _set_user_scope(conn, user_id)
                await conn.execute(
                    """
                    INSERT INTO monitored_controls (user_id, control_id, monitored, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (user_id, control_id) DO UPDATE
                       SET monitored = EXCLUDED.monitored,
                           updated_at = now()
                    """,
                    (user_id, control_id, monitored),
                )


__all__ = [
    "ControlSnapshot",
    "DriftDetectionOutcome",
    "compute_event_content_hash",
    "detect_drift",
    "dismiss_drift_event",
    "list_drift_events_for_user",
    "list_monitored_optouts",
    "resolve_drift_event",
    "set_monitored",
]
