"""Mock readiness challenge routes — Sprint 8 chunks 8.5, 8.6, 8.7.

POST   /api/mock-audit/run                — start a new run (enqueues mock_audit.run)
GET    /api/mock-audit                    — list runs for the current user
GET    /api/mock-audit/{run_id}           — full run with findings
GET    /api/mock-audit/{run_id}/poll      — JSON polling fallback
GET    /api/mock-audit/{run_id}/events    — SSE stream of run updates (LISTEN/NOTIFY)
GET    /api/mock-audit/{run_id}/report    — 302 to pre-signed gap report URL

Refs: PLAN.md chunks 8.5-8.7; ADR-0002, ADR-0010; system-design 11.3.
US-019, US-020.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from apps.api.auth.clerk import ClerkUser, verify_clerk_token
from apps.api.db import AppDbPool, AppDbPoolDep
from apps.api.jobs import JobMessage, JobQueue, JobType
from apps.api.services.object_storage import ObjectStorage, get_object_storage

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
router = APIRouter(tags=["mock-audit"])

_mock_audit_limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _mock_audit_rate_limit() -> str:
    return os.environ.get("MOCK_AUDIT_RATE_LIMIT", "10/minute")


# ── Schemas ──────────────────────────────────────────────────────────────────


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scan_run_id: str | None = Field(default=None, max_length=128)


class StartRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    task_id: str
    status: str = "queued"
    deduplicated: bool = False


class FindingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    run_id: str
    severity: str
    tsc_id: str | None = None
    objection: str
    recommended_next_step: str
    sequence_idx: int


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    user_id: str
    scan_run_id: str | None = None
    status: str
    summary: str = ""
    findings_count: int = 0
    severity_max: str = "none"
    spent_usd: float = 0.0
    cap_usd: float = 0.0
    a2a_task_id: str | None = None
    report_r2_key: str | None = None
    failure_reason: str | None = None
    created_at: str
    updated_at: str


class RunListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: list[RunSummary] = Field(default_factory=list)
    count: int = 0


class RunDetailOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: RunSummary
    findings: list[FindingOut] = Field(default_factory=list)


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _set_user_scope(conn: Any, user_id: str) -> None:
    await conn.execute("SELECT set_config('app.current_user_id', %s, true)", (user_id,))


async def _insert_run(
    pool: AppDbPool,
    *,
    user_id: str,
    scan_run_id: str | None,
    cap_usd: float,
    job_idempotency_key: str,
) -> str:
    run_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            await conn.execute(
                """
                INSERT INTO mock_audit_runs
                    (id, user_id, scan_run_id, status, cap_usd, job_idempotency_key)
                VALUES
                    (%s::uuid, %s, %s::uuid, 'queued', %s, %s)
                """,
                (run_id, user_id, scan_run_id, cap_usd, job_idempotency_key),
            )
    return run_id


async def _find_run_by_idempotency_key(
    pool: AppDbPool, *, user_id: str, job_idempotency_key: str
) -> str | None:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id::text FROM mock_audit_runs
                    WHERE user_id = %s
                      AND job_idempotency_key = %s
                      AND status NOT IN ('failed', 'completed', 'budget_exceeded')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (user_id, job_idempotency_key),
                )
                row = await cur.fetchone()
    return row[0] if row else None


async def _list_runs(pool: AppDbPool, *, user_id: str) -> list[RunSummary]:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id::text, user_id, scan_run_id::text, status, summary,
                           findings_count, severity_max, spent_usd, cap_usd,
                           a2a_task_id, report_r2_key, failure_reason,
                           created_at, updated_at
                    FROM mock_audit_runs
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 100
                    """,
                    (user_id,),
                )
                rows = await cur.fetchall()
    return [_row_to_run_summary(r) for r in rows]


async def _fetch_run(
    pool: AppDbPool, *, user_id: str, run_id: str
) -> RunSummary | None:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id::text, user_id, scan_run_id::text, status, summary,
                           findings_count, severity_max, spent_usd, cap_usd,
                           a2a_task_id, report_r2_key, failure_reason,
                           created_at, updated_at
                    FROM mock_audit_runs
                    WHERE user_id = %s AND id = %s::uuid
                    """,
                    (user_id, run_id),
                )
                row = await cur.fetchone()
    return _row_to_run_summary(row) if row else None


async def _list_findings(
    pool: AppDbPool, *, user_id: str, run_id: str
) -> list[FindingOut]:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id::text, run_id::text, severity, tsc_id, objection,
                           recommended_next_step, sequence_idx
                    FROM mock_audit_findings
                    WHERE user_id = %s AND run_id = %s::uuid
                    ORDER BY sequence_idx ASC
                    """,
                    (user_id, run_id),
                )
                rows = await cur.fetchall()
    return [_row_to_finding(r) for r in rows]


def _row_to_run_summary(row: tuple) -> RunSummary:
    (
        id_,
        user_id,
        scan_run_id,
        run_status,
        summary,
        findings_count,
        severity_max,
        spent_usd,
        cap_usd,
        a2a_task_id,
        report_r2_key,
        failure_reason,
        created_at,
        updated_at,
    ) = row
    return RunSummary(
        id=id_,
        user_id=user_id,
        scan_run_id=scan_run_id,
        status=run_status,
        summary=summary or "",
        findings_count=findings_count or 0,
        severity_max=severity_max or "none",
        spent_usd=float(spent_usd or 0.0),
        cap_usd=float(cap_usd or 0.0),
        a2a_task_id=a2a_task_id,
        report_r2_key=report_r2_key,
        failure_reason=failure_reason,
        created_at=_iso(created_at),
        updated_at=_iso(updated_at),
    )


def _row_to_finding(row: tuple) -> FindingOut:
    id_, run_id, severity, tsc_id, objection, next_step, seq_idx = row
    return FindingOut(
        id=id_,
        run_id=run_id,
        severity=severity,
        tsc_id=tsc_id,
        objection=objection or "",
        recommended_next_step=next_step or "",
        sequence_idx=int(seq_idx or 0),
    )


def _iso(dt: datetime | str | None) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


# ── Routes ───────────────────────────────────────────────────────────────────


def _get_storage() -> ObjectStorage:
    return get_object_storage()


def _get_job_queue() -> JobQueue:
    from apps.api.main import get_job_queue as _gjq

    return _gjq()


def _budget_cap() -> float:
    from apps.api.main import get_settings

    settings = get_settings()
    return float(settings.mock_audit_budget_usd or settings.llm_budget_cap_usd)


@router.post("/api/mock-audit/run", response_model=StartRunResponse)
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def start_run(
    request: Request,
    body: StartRunRequest,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
    queue: Annotated[JobQueue, Depends(_get_job_queue)],
) -> StartRunResponse:
    """Start a mock readiness challenge run (chunk 8.5 / 8.6)."""
    with tracer.start_as_current_span("mock_audit.start_run") as span:
        span.set_attribute("user.id", user.user_id)
        cap = _budget_cap()
        idempotency_key = f"mock_audit.run:{user.user_id}:{body.scan_run_id or 'latest'}"

        existing = await _find_run_by_idempotency_key(
            pool, user_id=user.user_id, job_idempotency_key=idempotency_key
        )
        if existing is not None:
            return StartRunResponse(
                run_id=existing,
                task_id=f"dedup:{idempotency_key}",
                status="queued",
                deduplicated=True,
            )

        run_id = await _insert_run(
            pool,
            user_id=user.user_id,
            scan_run_id=body.scan_run_id,
            cap_usd=cap,
            job_idempotency_key=idempotency_key,
        )
        message = JobMessage(
            type=JobType.MOCK_AUDIT_RUN,
            user_id=user.user_id,
            idempotency_key=idempotency_key,
            payload={
                "run_id": run_id,
                "scan_run_id": body.scan_run_id,
                "cap_usd": cap,
            },
        )
        result = await queue.enqueue(message)
        return StartRunResponse(
            run_id=run_id,
            task_id=result.message_id,
            status="queued",
            deduplicated=result.deduplicated,
        )


@router.get("/api/mock-audit", response_model=RunListOut)
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def list_runs(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> RunListOut:
    runs = await _list_runs(pool, user_id=user.user_id)
    return RunListOut(runs=runs, count=len(runs))


@router.get("/api/mock-audit/{run_id}", response_model=RunDetailOut)
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def get_run(
    request: Request,
    run_id: str,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> RunDetailOut:
    run = await _fetch_run(pool, user_id=user.user_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    findings = await _list_findings(pool, user_id=user.user_id, run_id=run_id)
    return RunDetailOut(run=run, findings=findings)


@router.get("/api/mock-audit/{run_id}/poll", response_model=RunSummary)
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def poll_run(
    request: Request,
    run_id: str,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> RunSummary:
    run = await _fetch_run(pool, user_id=user.user_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/api/mock-audit/{run_id}/events")
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def stream_run_events(
    request: Request,
    run_id: str,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> StreamingResponse:
    """SSE stream of mock_audit_run updates (chunk 8.6).

    Mirrors the questionnaire SSE bridge: subscribe to the
    ``mock_audit_run_updates`` Postgres channel via LISTEN, filter by
    ``user_id`` + ``run_id`` server-side, push each matching event as a
    ``data:`` JSON frame. Closes when the run reaches a terminal status.
    Rate-limited to keep an attacker from holding many connections open.
    """
    span = tracer.start_as_current_span("mock_audit.stream_run_events")
    with span as s:
        s.set_attribute("user.id", user.user_id)
        s.set_attribute("mock_audit.run_id", run_id)
        run = await _fetch_run(pool, user_id=user.user_id, run_id=run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
            )

    async def event_stream():
        yield _sse_frame("data-mock-audit-status", run.model_dump())
        terminal = {"completed", "failed", "budget_exceeded"}
        if run.status in terminal:
            yield "data: [DONE]\n\n"
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        listener = await _start_pg_listener(
            pool, user_id=user.user_id, run_id=run_id, queue=queue
        )
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse_frame("data-mock-audit-status", payload)
                if payload.get("status") in terminal:
                    break
        finally:
            await listener.stop()
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/mock-audit/{run_id}/report", response_model=None)
@_mock_audit_limiter.limit(_mock_audit_rate_limit)
async def download_report(
    request: Request,
    run_id: str,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
    storage: Annotated[ObjectStorage, Depends(_get_storage)],
) -> RedirectResponse | StreamingResponse:
    """Return the Markdown gap report (chunk 8.7).

    R2-backed: 302 to a 15-minute pre-signed URL.
    Local-fs-backed (dev/tests): stream bytes inline.
    """
    run = await _fetch_run(pool, user_id=user.user_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not run.report_r2_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Report not ready (status={run.status})",
        )
    if storage.backend == "local":
        body = await asyncio.to_thread(storage.get_bytes, run.report_r2_key)
        filename = f"mock-audit-{run_id}.md"
        return StreamingResponse(
            io.BytesIO(body),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    url = await asyncio.to_thread(
        storage.presigned_get_url, run.report_r2_key, ttl_seconds=900
    )
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


# ── Postgres LISTEN/NOTIFY bridge (mirrors questionnaire route) ─────────────


class _PgListener:
    def __init__(self, conn_ctx: Any, task: asyncio.Task) -> None:
        self._conn_ctx = conn_ctx
        self._task = task

    async def stop(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await self._conn_ctx.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.exception("mock_audit.pg_listener.close_failed")


async def _start_pg_listener(
    pool: AppDbPool,
    *,
    user_id: str,
    run_id: str,
    queue: asyncio.Queue,
) -> _PgListener:
    conn_ctx = pool.connection()
    conn = await conn_ctx.__aenter__()
    await conn.execute("LISTEN mock_audit_run_updates")
    await conn.commit()

    async def reader_loop() -> None:
        try:
            async for notify in conn.notifies():
                try:
                    data = json.loads(notify.payload)
                except (ValueError, AttributeError):
                    continue
                if data.get("user_id") != user_id:
                    continue
                if data.get("run_id") != run_id:
                    continue
                await queue.put(data)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("mock_audit.pg_listener.reader_failed run_id=%s", run_id)

    task = asyncio.create_task(reader_loop(), name=f"mock-audit-listen-{run_id}")
    return _PgListener(conn_ctx, task)


def _sse_frame(channel: str, data: dict[str, Any]) -> str:
    payload = {"type": channel, **data}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


__all__ = [
    "FindingOut",
    "RunDetailOut",
    "RunListOut",
    "RunSummary",
    "StartRunRequest",
    "StartRunResponse",
    "router",
]
