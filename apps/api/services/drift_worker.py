"""Background worker handler for ``drift.scan`` jobs (Sprint 9 chunks 9.4, 9.6).

The Vercel Cron-driven cycle:

  POST /api/internal/forward-drift  (FE proxy adds X-Cron-Token)
       └─> POST /api/drift/run      (FastAPI; verifies token)
              └─> enqueues drift.scan jobs (one per active user)
                     └─> this handler runs per user in the worker

Per call this handler:

  1. Loads the user's most recent ``control_map_cache`` rows + their
     associated evidence projections.
  2. Builds a list of :class:`ControlSnapshot` (one per monitored TSC).
  3. Hands the list to :func:`detect_drift` which applies 2-scan flap
     protection + re-fire suppression and writes to ``drift_events``.

Refs: PLAN.md chunks 9.4, 9.5, 9.6, 9.9; ADR-0010; system-design 13.5.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

from apps.api.jobs import JobMessage
from apps.api.jobs.exceptions import FatalError, RetryableError
from apps.api.services.drift import (
    ControlSnapshot,
    DriftDetectionOutcome,
    detect_drift,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


SnapshotsLoaderFn = Callable[
    [str, AsyncConnectionPool], Awaitable[list[ControlSnapshot]]
]


class DriftScanHandler:
    """Async handler for ``drift.scan`` job messages."""

    def __init__(
        self,
        *,
        pool_factory: Callable[[], AsyncConnectionPool | None],
        snapshots_loader: SnapshotsLoaderFn | None = None,
    ) -> None:
        self._pool_factory = pool_factory
        self._loader = snapshots_loader or self._default_snapshots_loader

    async def __call__(self, message: JobMessage) -> None:
        with tracer.start_as_current_span("drift.scan") as span:
            span.set_attribute("user.id", message.user_id)
            try:
                outcome = await self._run(message.user_id)
            except FatalError:
                raise
            except RetryableError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("drift.scan failed user_id=%s", message.user_id)
                raise FatalError(str(exc)) from exc
            span.set_attribute("drift.snapshots_seen", outcome.snapshots_seen)
            span.set_attribute("drift.events_emitted", outcome.events_emitted)
            span.set_attribute(
                "drift.events_suppressed_by_content_hash",
                outcome.events_suppressed_by_content_hash,
            )

    async def _run(self, user_id: str) -> DriftDetectionOutcome:
        pool = self._pool_factory()
        if pool is None:
            raise RetryableError("DB pool not available")
        snapshots = await self._loader(user_id, pool)
        return await detect_drift(pool, user_id=user_id, snapshots=snapshots)

    async def _default_snapshots_loader(
        self, user_id: str, pool: AsyncConnectionPool
    ) -> list[ControlSnapshot]:
        """Build a snapshot list from control_map_cache + evidence.

        Sprint 9 keeps this simple: read the most recent control_map_cache
        rows and project their evidence's raw payloads. If the user has
        run zero scans, the list is empty (and the detector emits no
        events). The orchestrator's future ``scan_run_snapshot`` table
        will replace this with a per-run frozen view.
        """

        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    (user_id,),
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT control_id, status, evidence_ids
                        FROM control_map_cache
                        WHERE user_id = %s
                        ORDER BY computed_at DESC
                        LIMIT 200
                        """,
                        (user_id,),
                    )
                    cache_rows = await cur.fetchall()

                # Pick a representative evidence row per control: the
                # newest content_hash that references the control_id.
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT id::text, raw, source_uri
                        FROM evidence
                        WHERE user_id = %s
                        ORDER BY collected_at DESC
                        LIMIT 200
                        """,
                        (user_id,),
                    )
                    ev_rows = await cur.fetchall()

        # Group evidence by control_id: cache row's evidence_ids gives
        # us the link.
        ev_by_id = {r[0]: {"raw": r[1] or {}, "source_uri": r[2]} for r in ev_rows}
        snapshots: list[ControlSnapshot] = []
        for control_id, status, evidence_ids in cache_rows:
            chosen: dict[str, Any] | None = None
            source_link: str | None = None
            for eid in evidence_ids or []:
                payload = ev_by_id.get(str(eid))
                if payload is not None:
                    chosen = payload["raw"]
                    source_link = payload["source_uri"]
                    break
            if chosen is None:
                # No evidence rows reachable yet: use a sentinel projection
                # carrying the status. This still allows status_changed
                # detection across runs.
                chosen = {"_assessed_status": status}
            snapshots.append(
                ControlSnapshot(
                    control_id=control_id,
                    projection=chosen,
                    source_link=source_link,
                    status_label=status,
                )
            )
        return snapshots


__all__ = [
    "DriftScanHandler",
    "SnapshotsLoaderFn",
]
