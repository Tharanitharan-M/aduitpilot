"""Background worker handler for ``mock_audit.run`` jobs (Sprint 8 chunk 8.5).

Workflow:
  1. Load the run row + scan context (control_map snapshot + evidence).
  2. Move ``mock_audit_runs.status`` ``queued`` -> ``dispatching``.
  3. Send the scan context as one A2A v1.0 task to the AdversarialAuditor
     via :class:`RemoteA2aAgent` (chunk 8.4).
  4. Move ``status`` ``dispatching`` -> ``running`` once the remote task
     starts. Poll until terminal.
  5. Persist the returned findings into ``mock_audit_findings``; mark
     the run ``completed``, ``failed``, or ``budget_exceeded`` based on
     the A2A task state.
  6. Hand the result to the gap-report assembler (chunk 8.7) which
     uploads the Markdown to object storage and stamps the
     ``report_r2_key`` column.

Each UPDATE on ``mock_audit_runs`` fires the pg_notify trigger from
0009_mock_audit_runs.sql, which the SSE bridge in
``routes/mock_audit.py`` forwards to the dashboard live.

Refs: PLAN.md chunks 8.5-8.7; ADR-0002, ADR-0010.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

from apps.api.agents.remote_auditor import (
    A2ATaskResult,
    RemoteA2aAgent,
    RemoteAuditorError,
)
from apps.api.jobs import JobMessage
from apps.api.jobs.exceptions import FatalError, RetryableError
from apps.api.services.gap_report import GapReportContext, render_gap_report
from apps.api.services.object_storage import ObjectStorage

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

ScanContextLoaderFn = Callable[[str, str], "Awaitable[dict[str, Any] | None]"]
RemoteAgentFactoryFn = Callable[[], "Awaitable[RemoteA2aAgent]"]


SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _max_severity(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "none"
    return max(
        (str(f.get("severity", "low")) for f in findings),
        key=lambda s: SEVERITY_RANK.get(s, 0),
    )


class MockAuditRunHandler:
    """Async handler for ``mock_audit.run`` job messages."""

    def __init__(
        self,
        *,
        pool_factory: Callable[[], AsyncConnectionPool | None],
        storage: ObjectStorage,
        remote_agent_factory: RemoteAgentFactoryFn,
        scan_context_loader: ScanContextLoaderFn | None = None,
    ) -> None:
        self._pool_factory = pool_factory
        self._storage = storage
        self._remote_agent_factory = remote_agent_factory
        self._scan_context_loader = scan_context_loader or self._default_scan_context_loader

    async def __call__(self, message: JobMessage) -> None:
        with tracer.start_as_current_span("mock_audit.run") as span:
            span.set_attribute("user.id", message.user_id)
            payload = message.payload
            run_id = str(payload.get("run_id", ""))
            if not run_id:
                raise FatalError("mock_audit.run payload missing run_id")
            span.set_attribute("mock_audit.run_id", run_id)
            try:
                await self._run(message.user_id, run_id, payload)
            except FatalError:
                raise
            except RetryableError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("mock_audit.run failed run_id=%s", run_id)
                await self._mark_failed(message.user_id, run_id, str(exc)[:500])
                raise FatalError(str(exc)) from exc

    async def _run(self, user_id: str, run_id: str, payload: dict[str, Any]) -> None:
        await self._update_status(user_id, run_id, "dispatching")
        scan_run_id = payload.get("scan_run_id")
        scan_context = await self._scan_context_loader(user_id, str(scan_run_id or ""))
        if scan_context is None:
            scan_context = {"control_map": {}, "evidence": [], "scan_run_id": None}

        agent = await self._remote_agent_factory()
        try:
            await self._update_status(user_id, run_id, "running")
            try:
                result = await agent.send_message(scan_context)
            except RemoteAuditorError as exc:
                await self._mark_failed(user_id, run_id, f"a2a: {exc}")
                raise FatalError(str(exc)) from exc
        finally:
            await agent.aclose()

        await self._persist_findings(user_id, run_id, result)
        report_key = await self._upload_report(user_id, run_id, result, scan_context)
        await self._mark_terminal(user_id, run_id, result, report_key)

    # ─── DB helpers ──────────────────────────────────────────────────────────

    def _pool(self) -> AsyncConnectionPool:
        pool = self._pool_factory()
        if pool is None:
            raise RetryableError("DB pool not available")
        return pool

    async def _update_status(self, user_id: str, run_id: str, new_status: str) -> None:
        with tracer.start_as_current_span("db.mock_audit_runs.update_status") as span:
            span.set_attribute("db.table", "mock_audit_runs")
            span.set_attribute("mock_audit.new_status", new_status)
            pool = self._pool()
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.current_user_id', %s, true)", (user_id,)
                    )
                    await conn.execute(
                        """
                        UPDATE mock_audit_runs
                        SET status = %s, updated_at = now()
                        WHERE user_id = %s AND id = %s::uuid
                        """,
                        (new_status, user_id, run_id),
                    )

    async def _persist_findings(
        self, user_id: str, run_id: str, result: A2ATaskResult
    ) -> None:
        with tracer.start_as_current_span("db.mock_audit_findings.persist") as span:
            span.set_attribute("db.table", "mock_audit_findings")
            span.set_attribute("mock_audit.findings_count", len(result.findings))
            pool = self._pool()
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.current_user_id', %s, true)", (user_id,)
                    )
                    await conn.execute(
                        "DELETE FROM mock_audit_findings "
                        "WHERE user_id = %s AND run_id = %s::uuid",
                        (user_id, run_id),
                    )
                    async with conn.cursor() as cur:
                        for idx, finding in enumerate(result.findings):
                            severity = str(finding.get("severity", "low"))
                            if severity not in SEVERITY_RANK or severity == "none":
                                severity = "low"
                            await cur.execute(
                                """
                                INSERT INTO mock_audit_findings
                                    (id, run_id, user_id, severity, tsc_id, objection,
                                     recommended_next_step, sequence_idx)
                                VALUES
                                    (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    str(uuid.uuid4()),
                                    run_id,
                                    user_id,
                                    severity,
                                    finding.get("tsc_id"),
                                    str(finding.get("objection", ""))[:8000],
                                    str(finding.get("recommended_next_step", ""))[:8000],
                                    idx,
                                ),
                            )

    async def _mark_terminal(
        self,
        user_id: str,
        run_id: str,
        result: A2ATaskResult,
        report_key: str | None,
    ) -> None:
        if result.state == "TASK_STATE_COMPLETED":
            new_status = "completed"
        elif result.state == "TASK_STATE_BUDGET_EXCEEDED":
            new_status = "budget_exceeded"
        else:
            new_status = "failed"
        max_sev = _max_severity(result.findings)
        spent = float(result.budget.get("spent_usd", 0.0)) if result.budget else 0.0
        cap = float(result.budget.get("cap_usd", 0.0)) if result.budget else 0.0
        with tracer.start_as_current_span("db.mock_audit_runs.mark_terminal") as span:
            span.set_attribute("db.table", "mock_audit_runs")
            span.set_attribute("mock_audit.terminal_status", new_status)
            await self._do_mark_terminal(
                user_id, run_id, new_status, result, report_key, max_sev, spent, cap
            )

    async def _do_mark_terminal(
        self,
        user_id: str,
        run_id: str,
        new_status: str,
        result: A2ATaskResult,
        report_key: str | None,
        max_sev: str,
        spent: float,
        cap: float,
    ) -> None:
        pool = self._pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)", (user_id,)
                )
                await conn.execute(
                    """
                    UPDATE mock_audit_runs
                    SET status = %s,
                        summary = %s,
                        findings_count = %s,
                        severity_max = %s,
                        spent_usd = %s,
                        cap_usd = %s,
                        a2a_task_id = %s,
                        report_r2_key = %s,
                        failure_reason = %s,
                        updated_at = now()
                    WHERE user_id = %s AND id = %s::uuid
                    """,
                    (
                        new_status,
                        result.summary[:4000],
                        len(result.findings),
                        max_sev,
                        spent,
                        cap,
                        result.task_id,
                        report_key,
                        (result.error or "")[:1000] if result.error else None,
                        user_id,
                        run_id,
                    ),
                )

    async def _mark_failed(self, user_id: str, run_id: str, reason: str) -> None:
        try:
            pool = self._pool()
        except RetryableError:
            return
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.current_user_id', %s, true)", (user_id,)
                    )
                    await conn.execute(
                        """
                        UPDATE mock_audit_runs
                        SET status = 'failed',
                            failure_reason = %s,
                            updated_at = now()
                        WHERE user_id = %s AND id = %s::uuid
                        """,
                        (reason, user_id, run_id),
                    )
        except Exception:  # noqa: BLE001
            logger.exception("mock_audit.run mark_failed_failed run_id=%s", run_id)

    async def _upload_report(
        self,
        user_id: str,
        run_id: str,
        result: A2ATaskResult,
        scan_context: dict[str, Any],
    ) -> str | None:
        ctx = GapReportContext(
            run_id=run_id,
            user_id=user_id,
            summary=result.summary,
            findings=result.findings,
            scan_context=scan_context,
            budget=result.budget,
            state=result.state,
            error=result.error,
        )
        body = render_gap_report(ctx).encode("utf-8")
        key = self._storage.make_key(
            user_id=user_id, kind="mock-audits", suffix=".md"
        )
        try:
            stored = await _to_thread(
                self._storage.put_bytes, key, body, content_type="text/markdown"
            )
        except Exception:  # noqa: BLE001
            logger.exception("mock_audit.report.upload_failed run_id=%s", run_id)
            return None
        return stored.key

    async def _default_scan_context_loader(
        self, user_id: str, scan_run_id: str
    ) -> dict[str, Any] | None:
        """Load a draft control_map snapshot for the user.

        Sprint 8 keeps the loader simple: pull the most recent
        ``control_map_cache`` rows for the user and project them as a
        flat dict the AdversarialAuditor can read. The orchestrator's
        future ``scan_run_snapshot`` table (Sprint 9) will replace this
        with a per-run snapshot.
        """
        pool = self._pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)", (user_id,)
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT control_id, status, confidence, rationale,
                               evidence_ids
                        FROM control_map_cache
                        WHERE user_id = %s
                        ORDER BY computed_at DESC
                        LIMIT 200
                        """,
                        (user_id,),
                    )
                    rows = await cur.fetchall()
        control_map = [
            {
                "control_id": r[0],
                "status": r[1],
                "confidence": float(r[2]),
                "rationale": r[3] or "",
                "evidence_ids": list(r[4]) if r[4] else [],
            }
            for r in rows
        ]
        return {
            "scan_run_id": scan_run_id or None,
            "control_map": control_map,
            "evidence": [],  # drift-watcher / evidence link comes in Sprint 9
        }


async def _to_thread[T](
    func: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Local indirection so tests can patch ``asyncio.to_thread``."""
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


__all__ = [
    "MockAuditRunHandler",
    "ScanContextLoaderFn",
    "_max_severity",
]
