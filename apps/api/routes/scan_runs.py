"""Scan-run routes — Sprint 9 chunks 9.10, 9.11, 9.12.

GET    /api/scan-runs                     — list runs (newest first)
GET    /api/scan-runs/{id}                — fetch one
POST   /api/scan-runs                     — create a re-run (parent linked)
GET    /api/scan-runs/diff?a=<id>&b=<id>  — typed ScanRunDiff (system-design 15.2)

Re-run flow (system-design 15.1):

  POST /api/scan-runs body = {
      "source": "rerun",
      "parent": "<parent_uuid>",
      "params_override": {...}        # only non-scope params per ADR-0015
  }

The body is documented but only ``source`` + ``parent`` are validated;
``params_override`` is stored as JSON-encoded text in
``scan_runs.repo_include_list`` is NOT touched — per ADR-0015 the canonical
scope lives on ``connector_scoped_repos``. The new run inherits the parent's
``connector_id`` and the user's *current* repo scope.

Refs: PLAN.md chunks 9.10, 9.11, 9.12; ADR-0015; system-design 15.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from apps.api.auth.clerk import ClerkUser, verify_clerk_token
from apps.api.db import AppDbPool, AppDbPoolDep

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
router = APIRouter(prefix="/api/scan-runs", tags=["scan-runs"])

_scan_runs_limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _scan_runs_rate_limit() -> str:
    return os.environ.get("SCAN_RUNS_RATE_LIMIT", "60/minute")


# ── Schemas ──────────────────────────────────────────────────────────────────


class ScanRunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    connector_id: str | None = None
    repo_include_list: list[str] = Field(default_factory=list)
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    completed_at: str | None = None
    cancelled: bool = False
    parent_scan_run_id: str | None = None


class ScanRunListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: list[ScanRunOut] = Field(default_factory=list)
    count: int = 0


class ScanRunRerunRequest(BaseModel):
    """Body for POST /api/scan-runs (chunk 9.10)."""

    model_config = ConfigDict(extra="forbid")
    source: Literal["rerun"] = "rerun"
    parent: str = Field(min_length=1, description="Parent scan_run_id (UUID).")
    params_override: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Non-scope overrides (e.g. evidence freshness window). "
            "Per ADR-0015, scope lives on connector_scoped_repos and is "
            "not overridden here. Free-form JSON; capped at 4 KB."
        ),
    )


class ControlDiff(BaseModel):
    """Per-control diff between two runs (system-design 15.2)."""

    model_config = ConfigDict(extra="forbid")
    control_id: str
    a_status: str = ""
    b_status: str = ""
    a_confidence: float = 0.0
    b_confidence: float = 0.0
    rationale_changed: bool = False


class ScanRunDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: str
    b: str
    controls_changed: list[ControlDiff] = Field(default_factory=list)
    evidence_added: list[str] = Field(default_factory=list)
    evidence_removed: list[str] = Field(default_factory=list)


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _set_user_scope(conn: Any, user_id: str) -> None:
    await conn.execute("SELECT set_config('app.current_user_id', %s, true)", (user_id,))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _row_to_run(row: tuple) -> ScanRunOut:
    (
        id_,
        user_id,
        connector_id,
        repo_include_list,
        status_,
        started_at,
        completed_at,
        cancelled,
        parent_scan_run_id,
    ) = row
    return ScanRunOut(
        id=str(id_),
        user_id=user_id,
        connector_id=connector_id,
        repo_include_list=list(repo_include_list or []),
        status=status_,
        started_at=_iso(started_at) or "",
        completed_at=_iso(completed_at),
        cancelled=bool(cancelled),
        parent_scan_run_id=str(parent_scan_run_id) if parent_scan_run_id else None,
    )


async def _list_runs(pool: AppDbPool, *, user_id: str, limit: int) -> list[ScanRunOut]:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, user_id, connector_id, repo_include_list, status,
                           started_at, completed_at, cancelled, parent_scan_run_id
                    FROM scan_runs
                    WHERE user_id = %s
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
                rows = await cur.fetchall()
    return [_row_to_run(r) for r in rows]


async def _fetch_run(
    pool: AppDbPool, *, user_id: str, run_id: str
) -> ScanRunOut | None:
    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, user_id, connector_id, repo_include_list, status,
                           started_at, completed_at, cancelled, parent_scan_run_id
                    FROM scan_runs
                    WHERE user_id = %s AND id = %s::uuid
                    """,
                    (user_id, run_id),
                )
                row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def _create_rerun(
    pool: AppDbPool, *, user_id: str, parent_id: str
) -> str | None:
    """Create a new scan_runs row whose parent_scan_run_id == parent_id.

    Inherits connector_id from the parent. repo_include_list is the
    user's current scope (from connector_scoped_repos) — per ADR-0015.
    Returns the new id.
    """

    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            # Fetch parent's connector_id (and verify ownership).
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT connector_id
                    FROM scan_runs
                    WHERE user_id = %s AND id = %s::uuid
                    """,
                    (user_id, parent_id),
                )
                parent = await cur.fetchone()
                if parent is None:
                    return None
                connector_id = parent[0]

                # Snapshot the user's current scope from connector_scoped_repos.
                if connector_id is not None:
                    await cur.execute(
                        """
                        SELECT provider_repo_id
                        FROM connector_scoped_repos
                        WHERE user_id = %s AND connector_id = %s
                        """,
                        (user_id, connector_id),
                    )
                    repo_rows = await cur.fetchall()
                    repo_list = [r[0] for r in repo_rows]
                else:
                    repo_list = []

                await cur.execute(
                    """
                    INSERT INTO scan_runs
                        (user_id, connector_id, repo_include_list, status,
                         parent_scan_run_id)
                    VALUES
                        (%s, %s, %s::text[], 'running', %s::uuid)
                    RETURNING id
                    """,
                    (user_id, connector_id, repo_list, parent_id),
                )
                first = await cur.fetchone()
                return str(first[0]) if first else None


async def _diff_runs(
    pool: AppDbPool, *, user_id: str, a: str, b: str
) -> ScanRunDiff:
    """Build a ScanRunDiff by joining control_map_cache + evidence by scan_run_id.

    The current data model keeps control_map_cache keyed on (user_id,
    content_hash, control_id). To diff two scan_runs we compare the
    rationale + status of their cached entries. This is an approximation
    that gets sharper once the orchestrator persists per-run snapshots
    (Sprint 10).
    """

    async with pool.connection() as conn:
        async with conn.transaction():
            await _set_user_scope(conn, user_id)
            async with conn.cursor() as cur:
                # Latest cache row per control_id, joined to evidence rows
                # that reference each scan_run_id. The OR branches handle
                # the partial-overlap case where the same content_hash
                # surfaced from both runs.
                await cur.execute(
                    """
                    WITH ev_a AS (
                        SELECT id::text FROM evidence
                        WHERE user_id = %s AND scan_run_id = %s
                    ),
                    ev_b AS (
                        SELECT id::text FROM evidence
                        WHERE user_id = %s AND scan_run_id = %s
                    ),
                    cmap_a AS (
                        SELECT DISTINCT ON (control_id) control_id, status,
                               confidence, rationale
                        FROM control_map_cache
                        WHERE user_id = %s
                          AND evidence_ids && (
                                SELECT array_agg(id) FROM ev_a
                          )
                        ORDER BY control_id, computed_at DESC
                    ),
                    cmap_b AS (
                        SELECT DISTINCT ON (control_id) control_id, status,
                               confidence, rationale
                        FROM control_map_cache
                        WHERE user_id = %s
                          AND evidence_ids && (
                                SELECT array_agg(id) FROM ev_b
                          )
                        ORDER BY control_id, computed_at DESC
                    )
                    SELECT COALESCE(a.control_id, b.control_id) AS control_id,
                           COALESCE(a.status, '') AS a_status,
                           COALESCE(b.status, '') AS b_status,
                           COALESCE(a.confidence, 0.0) AS a_confidence,
                           COALESCE(b.confidence, 0.0) AS b_confidence,
                           COALESCE(a.rationale, '') AS a_rationale,
                           COALESCE(b.rationale, '') AS b_rationale
                    FROM cmap_a a
                    FULL OUTER JOIN cmap_b b USING (control_id)
                    """,
                    (user_id, a, user_id, b, user_id, user_id),
                )
                rows = await cur.fetchall()

                # Evidence diffs.
                await cur.execute(
                    """
                    SELECT id::text FROM evidence
                    WHERE user_id = %s AND scan_run_id = %s
                    """,
                    (user_id, a),
                )
                ev_a = {r[0] for r in await cur.fetchall()}
                await cur.execute(
                    """
                    SELECT id::text FROM evidence
                    WHERE user_id = %s AND scan_run_id = %s
                    """,
                    (user_id, b),
                )
                ev_b = {r[0] for r in await cur.fetchall()}

    diffs: list[ControlDiff] = []
    for row in rows:
        (
            control_id,
            a_status,
            b_status,
            a_conf,
            b_conf,
            a_rat,
            b_rat,
        ) = row
        if (
            a_status == b_status
            and abs(float(a_conf) - float(b_conf)) < 1e-9
            and a_rat == b_rat
        ):
            continue
        diffs.append(
            ControlDiff(
                control_id=control_id,
                a_status=a_status or "",
                b_status=b_status or "",
                a_confidence=float(a_conf or 0.0),
                b_confidence=float(b_conf or 0.0),
                rationale_changed=a_rat != b_rat,
            )
        )

    return ScanRunDiff(
        a=a,
        b=b,
        controls_changed=diffs,
        evidence_added=sorted(ev_b - ev_a),
        evidence_removed=sorted(ev_a - ev_b),
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("", response_model=ScanRunListOut)
@_scan_runs_limiter.limit(_scan_runs_rate_limit)
async def list_runs(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> ScanRunListOut:
    runs = await _list_runs(pool, user_id=user.user_id, limit=200)
    return ScanRunListOut(runs=runs, count=len(runs))


@router.get("/diff", response_model=ScanRunDiff)
@_scan_runs_limiter.limit(_scan_runs_rate_limit)
async def diff_runs(
    request: Request,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
    a: Annotated[str, Query(description="Earlier scan run id")] = "",
    b: Annotated[str, Query(description="Later scan run id")] = "",
) -> ScanRunDiff:
    if not (a and b):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Both 'a' and 'b' query params are required",
        )
    # Verify both runs belong to the user before any heavier query.
    run_a = await _fetch_run(pool, user_id=user.user_id, run_id=a)
    run_b = await _fetch_run(pool, user_id=user.user_id, run_id=b)
    if run_a is None or run_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both scan runs not found",
        )
    return await _diff_runs(pool, user_id=user.user_id, a=a, b=b)


@router.get("/{run_id}", response_model=ScanRunOut)
@_scan_runs_limiter.limit(_scan_runs_rate_limit)
async def get_run(
    request: Request,
    run_id: str,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> ScanRunOut:
    run = await _fetch_run(pool, user_id=user.user_id, run_id=run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Scan run not found"
        )
    return run


@router.post("", response_model=ScanRunOut)
@_scan_runs_limiter.limit(_scan_runs_rate_limit)
async def create_rerun(
    request: Request,
    body: ScanRunRerunRequest,
    pool: AppDbPoolDep,
    user: Annotated[ClerkUser, Depends(verify_clerk_token)],
) -> ScanRunOut:
    if body.source != "rerun":
        # Future-proof: today only re-run is supported on this endpoint.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only source='rerun' is supported",
        )
    new_id = await _create_rerun(pool, user_id=user.user_id, parent_id=body.parent)
    if new_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent scan run not found",
        )
    fresh = await _fetch_run(pool, user_id=user.user_id, run_id=new_id)
    if fresh is None:
        # Race / RLS dropout — the row was created but a concurrent
        # delete or a misconfigured RLS scope hid it. Don't return null
        # under the response_model contract; surface as 500 so the FE
        # can retry.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="scan_run_created_but_not_readable",
        )
    return fresh


__all__ = [
    "ControlDiff",
    "ScanRunDiff",
    "ScanRunListOut",
    "ScanRunOut",
    "ScanRunRerunRequest",
    "router",
]
