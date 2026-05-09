"""Drift watcher routes — Sprint 9 chunks 9.6, 9.8, 9.9.

POST   /api/drift/run                 — enqueue a drift.scan for the user
                                        (cron path requires X-Cron-Token)
GET    /api/drift/events              — list the user's drift events
PATCH  /api/drift/events/{event_id}   — dismiss / resolve a drift event
GET    /api/drift/monitored           — list controls explicitly opted out
PATCH  /api/drift/monitored/{control_id} — toggle monitoring for one TSC clause

Refs: PLAN.md chunks 9.5-9.9; ADR-0004 (read-only-by-design — these endpoints
write only AuditPilot's own state); system-design 13.

Cron flow:

  Vercel Cron  ->  FE proxy /api/internal/forward-drift (adds X-Cron-Token)
                   ->  this POST /api/drift/run
                   The proxy is needed because Vercel Cron does NOT support
                   custom request headers (chunk 9.7).

The FE proxy is the only caller authorised to pass an empty body and the
X-Cron-Token header — it then triggers a fan-out enqueue (one drift.scan
job per active user). Authenticated users can also call POST
/api/drift/run for their own scope (no cron token, just JWT).
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from drift_watcher_mcp.schemas import DriftEventOut
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from apps.api.auth.clerk import ClerkUser, verify_clerk_token
from apps.api.config import Settings
from apps.api.db import AppDbPool, AppDbPoolDep
from apps.api.jobs import JobMessage, JobQueue, JobType
from apps.api.services.drift import (
    DriftStatus,
    dismiss_drift_event,
    get_drift_status,
    list_drift_events_for_user,
    list_monitored_optouts,
    resolve_drift_event,
    set_monitored,
)

# Optional bearer guard for the cron-vs-user dual path on POST /api/drift/run.
# auto_error=False makes the dependency return None when no Authorization
# header is present, instead of raising 401 — which is exactly what we want
# for the cron path.
_optional_bearer = HTTPBearer(auto_error=False)


async def _optional_clerk_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    settings: Settings = Depends(lambda: Settings()),
) -> ClerkUser | None:
    """Return the verified ClerkUser when a Bearer is present, else None.

    Re-uses the canonical ``verify_clerk_token`` so we don't duplicate
    JWKS handling. When no header is supplied we return None and the
    cron path takes over.

    Note: ``verify_clerk_token`` itself takes ``settings`` as a
    Depends-injected default. When we call it directly from here we
    must pass ``settings`` explicitly — otherwise the parameter
    receives the unevaluated ``Depends(...)`` object and the function
    AttributeErrors on ``settings.clerk_issuer_url``, which the broad
    ``except`` block then masks as a generic 401. (Cause of the Sprint
    9 post-deploy "POST /api/drift/run 401" bug.)
    """

    if credentials is None:
        return None
    return await verify_clerk_token(credentials=credentials, settings=settings)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
router = APIRouter(prefix="/api/drift", tags=["drift"])

_drift_limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _drift_rate_limit() -> str:
    return os.environ.get("DRIFT_RATE_LIMIT", "60/minute")


def _drift_run_rate_limit() -> str:
    return os.environ.get("DRIFT_RUN_RATE_LIMIT", "10/minute")


# ── Schemas ──────────────────────────────────────────────────────────────────


DriftEventStatus = Literal["open", "resolved", "dismissed"]


class DriftEventOutAPI(BaseModel):
    """API-shape mirror of drift_watcher_mcp.schemas.DriftEventOut."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    control_id: str
    event_type: Literal["status_changed", "config_changed", "evidence_removed"]
    what_changed: str = ""
    previous_value: dict[str, Any] = Field(default_factory=dict)
    current_value: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str = ""
    source_link: str | None = None
    severity: Literal["low", "medium", "high"]
    detected_at: str
    status: DriftEventStatus
    content_hash: str = ""


class DriftEventListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[DriftEventOutAPI] = Field(default_factory=list)
    count: int = 0


class DriftEventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["dismissed", "resolved"]
    reason: str | None = Field(default=None, max_length=2000)


class DriftRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: list[str] | None = Field(
        default=None,
        description=(
            "Cron path: list of user_ids to enqueue. Authenticated path: "
            "ignored (we only enqueue for the caller). Length capped at 200 "
            "so the cron worker cannot fan-out unboundedly."
        ),
        max_length=200,
    )


class DriftRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enqueued: int
    deduplicated: int = Field(
        default=0,
        description=(
            "Count of jobs that were already in flight for the same minute "
            "and got coalesced. UI surfaces this so the user can tell a "
            "double-click apart from a fresh scan."
        ),
    )
    triggered_by: Literal["cron", "user"]


class MonitoredEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control_id: str
    monitored: bool


class DriftStatusOut(BaseModel):
    """Heartbeat panel response (post-Sprint-9 UX fix).

    Lets the dashboard distinguish "watcher ran with no events to
    report" from "job stuck in queue" — the page used to look identical
    in both cases.
    """

    model_config = ConfigDict(extra="forbid")

    baselines: int = Field(
        default=0,
        description="Number of (user, control_id) pairs the watcher has baselined.",
    )
    last_scan_at: str | None = Field(
        default=None,
        description="ISO-8601 timestamp of the most recent drift_snapshots write.",
    )
    events_total: int = 0
    events_open: int = 0


class MonitoredListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    optouts: list[str] = Field(default_factory=list)


class MonitoredPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monitored: bool


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_api(event: DriftEventOut) -> DriftEventOutAPI:
    detected = event.detected_at
    if isinstance(detected, datetime):
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=UTC)
        detected_str = detected.isoformat()
    else:
        detected_str = str(detected)
    return DriftEventOutAPI(
        id=event.id,
        user_id=event.user_id,
        control_id=event.control_id,
        event_type=event.event_type,
        what_changed=event.what_changed or "",
        previous_value=event.previous_value or {},
        current_value=event.current_value or {},
        suggested_fix=event.suggested_fix or "",
        source_link=event.source_link,
        severity=event.severity,
        detected_at=detected_str,
        status=event.status,
        content_hash=event.content_hash or "",
    )


def _verify_cron_token(provided: str | None) -> bool:
    """Constant-time match against the configured cron secret.

    Returns True when a configured ``CRON_SECRET`` matches; False
    otherwise. ``CRON_SECRET`` is also read from settings so dev
    overrides via ``.env`` work without re-importing.
    """

    expected = os.environ.get("CRON_SECRET", "")
    if not expected:
        return False
    if provided is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _get_job_queue() -> JobQueue:
    from apps.api.main import get_job_queue as _gjq

    return _gjq()


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/run", response_model=DriftRunResponse)
@_drift_limiter.limit(_drift_run_rate_limit)
async def run_drift(
    request: Request,
    body: DriftRunRequest,
    pool: AppDbPoolDep,
    queue: Annotated[JobQueue, Depends(_get_job_queue)],
    user: Annotated[ClerkUser | None, Depends(_optional_clerk_user)] = None,
    x_cron_token: Annotated[str | None, Header(alias="X-Cron-Token")] = None,
) -> DriftRunResponse:
    """Enqueue ``drift.scan`` jobs.

    Two paths:

      * **Cron**   — request carries ``X-Cron-Token``. We fan-out one job
        per user_id in the request body (capped) OR, when no body is
        supplied, one job per active user discovered from
        ``connector_scoped_repos``. No JWT required because the proxy is
        the only caller that can present the secret.
      * **User**   — request has a Clerk JWT. We enqueue one job for the
        caller; ``user_ids`` in the body is ignored.
    """

    if x_cron_token is not None:
        if not _verify_cron_token(x_cron_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid cron token",
            )
        target_user_ids = body.user_ids or await _list_active_user_ids(pool)
        with tracer.start_as_current_span("drift.run.cron") as span:
            span.set_attribute("drift.fan_out", len(target_user_ids))
            enqueued, deduplicated = await _enqueue_many(queue, target_user_ids)
            span.set_attribute("drift.enqueued", enqueued)
            span.set_attribute("drift.deduplicated", deduplicated)
        return DriftRunResponse(
            enqueued=enqueued, deduplicated=deduplicated, triggered_by="cron"
        )

    # Authenticated path (no cron token) — JWT must be valid (the
    # optional dependency returns None only when the header is absent).
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with tracer.start_as_current_span("drift.run.user") as span:
        span.set_attribute("user.id", user.user_id)
        enqueued, deduplicated = await _enqueue_many(queue, [user.user_id])
        span.set_attribute("drift.enqueued", enqueued)
        span.set_attribute("drift.deduplicated", deduplicated)
    return DriftRunResponse(
        enqueued=enqueued, deduplicated=deduplicated, triggered_by="user"
    )


async def _list_active_user_ids(pool: AppDbPool) -> list[str]:
    """Return the set of user_ids that have at least one connector_scoped_repos row.

    Cron service-scope: this is the ONE query in the codebase that
    deliberately spans tenants — the cron has no single user context
    and must enumerate every user with connectors so each gets its own
    `drift.scan` job. We mark this with a sentinel ``__cron__`` value
    in ``app.current_user_id`` so the RLS policy on
    ``connector_scoped_repos`` is exercised predictably even if the
    Postgres role drops BYPASSRLS in production. The policy is written
    so a NULL/sentinel scope produces zero rows; today's role has
    BYPASSRLS so the query returns all rows. database-reviewer
    CRITICAL-2: when we tighten the role, switch to a dedicated
    service_role for this query path.
    """

    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    ("__cron__",),
                )
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT DISTINCT user_id
                        FROM connector_scoped_repos
                        LIMIT 200
                        """
                    )
                    rows = await cur.fetchall()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        logger.exception("drift.list_active_user_ids.failed")
        return []


async def _enqueue_many(queue: JobQueue, user_ids: list[str]) -> tuple[int, int]:
    """Enqueue one drift.scan per user id. Returns (enqueued, deduplicated).

    The idempotency key is bucketed by minute so two clicks in the
    same 60-second window collapse to one job (prevents double-click
    spam) but a click 1+ minute later fires a fresh scan. Without the
    minute bucket, a permanent ``drift.scan:{uid}`` key dedups every
    subsequent click against the very first run forever — which is
    why the heartbeat appeared "stuck on the first scan."
    """

    minute_bucket = int(time.time() // 60)
    enqueued = 0
    deduplicated = 0
    for uid in user_ids:
        if not uid:
            continue
        message = JobMessage(
            type=JobType.DRIFT_SCAN,
            user_id=uid,
            idempotency_key=f"drift.scan:{uid}:{minute_bucket}",
            payload={},
        )
        result = await queue.enqueue(message)
        if getattr(result, "deduplicated", False):
            deduplicated += 1
        else:
            enqueued += 1
    return enqueued, deduplicated


@router.get("/events", response_model=DriftEventListOut)
@_drift_limiter.limit(_drift_rate_limit)
async def list_events(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
    status_filter: Annotated[DriftEventStatus | None, Query(alias="status")] = None,
    limit: int = 200,
) -> DriftEventListOut:
    limit = max(1, min(limit, 500))
    events = await list_drift_events_for_user(
        pool, user_id=user.user_id, status=status_filter, limit=limit
    )
    api_events = [_to_api(e) for e in events]
    return DriftEventListOut(events=api_events, count=len(api_events))


@router.patch("/events/{event_id}", response_model=DriftEventOutAPI)
@_drift_limiter.limit(_drift_rate_limit)
async def patch_event(
    request: Request,
    event_id: str,
    body: DriftEventPatch,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> DriftEventOutAPI:
    if body.status == "dismissed":
        if not (body.reason and body.reason.strip()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "reason_required", "to_status": "dismissed"},
            )
        updated = await dismiss_drift_event(
            pool, user_id=user.user_id, event_id=event_id, reason=body.reason
        )
    else:
        updated = await resolve_drift_event(
            pool, user_id=user.user_id, event_id=event_id
        )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Drift event not found or not in 'open' state",
        )
    return _to_api(updated)


@router.get("/status", response_model=DriftStatusOut)
@_drift_limiter.limit(_drift_rate_limit)
async def get_status(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> DriftStatusOut:
    """Heartbeat: confirm the watcher actually ran on a baseline-only scan."""
    snap: DriftStatus = await get_drift_status(pool, user_id=user.user_id)
    last_scan_iso: str | None = None
    if snap.last_scan_at is not None:
        ts = snap.last_scan_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        last_scan_iso = ts.isoformat()
    return DriftStatusOut(
        baselines=snap.baselines,
        last_scan_at=last_scan_iso,
        events_total=snap.events_total,
        events_open=snap.events_open,
    )


@router.get("/monitored", response_model=MonitoredListOut)
@_drift_limiter.limit(_drift_rate_limit)
async def get_monitored(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> MonitoredListOut:
    optouts = await list_monitored_optouts(pool, user_id=user.user_id)
    return MonitoredListOut(optouts=sorted(optouts))


_CONTROL_ID_PATH = Path(
    description="SOC 2 TSC clause id, e.g. 'CC6.1' or 'A1.2'.",
    min_length=1,
    max_length=64,
    # security-reviewer F5 — restrict to alphanumerics + . _ - so a
    # caller cannot persist arbitrary strings into monitored_controls
    # that would later surface in a CSV / LLM context.
    pattern=r"^[A-Z][A-Z0-9._-]{0,63}$",
)


@router.patch("/monitored/{control_id}", response_model=MonitoredEntry)
@_drift_limiter.limit(_drift_rate_limit)
async def patch_monitored(
    request: Request,
    control_id: Annotated[str, _CONTROL_ID_PATH],
    body: MonitoredPatch,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> MonitoredEntry:
    await set_monitored(
        pool,
        user_id=user.user_id,
        control_id=control_id,
        monitored=body.monitored,
    )
    return MonitoredEntry(control_id=control_id, monitored=body.monitored)


__all__ = [
    "DriftEventListOut",
    "DriftEventOutAPI",
    "DriftEventPatch",
    "DriftRunRequest",
    "DriftRunResponse",
    "MonitoredEntry",
    "MonitoredListOut",
    "MonitoredPatch",
    "router",
]
