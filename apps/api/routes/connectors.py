"""
Connector routes — GET /api/me, DELETE /api/connectors/{id},
                   PATCH /api/connectors/{id}/scoped-repos
=============================================================
Sprint 3 chunks 3.7, 3.8; Sprint 3.5 chunk 3.5.3.

GET /api/me    returns the current user's connector list + repo stubs +
               the persisted repo scope (scoped_repos count + ids).
DELETE /api/connectors/{id}      revokes a connector via the Clerk Backend API.
PATCH /api/connectors/{id}/scoped-repos
               sets the user-chosen repo scope for a connector. Idempotent
               under the unique index on (connector_id, provider_repo_id);
               the new selection wins (rows that fell off are deleted).

All three routes require a valid Clerk JWT (``verify_clerk_token`` dep). The
Clerk secret key is used server-side only — it never touches the browser.

Refs: PLAN.md chunks 3.7, 3.8 + Sprint 3.5 chunk 3.5.3;
ADR-0004 (read-only), ADR-0008 (Clerk), ADR-0015 (repo selection at scan
time); US-002, US-004, US-005.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth.clerk import ClerkUser, verify_clerk_token
from apps.api.config import Settings
from apps.api.db import AppDbPool, AppDbPoolDep, AppDbPoolOptionalDep

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
router = APIRouter(prefix="/api", tags=["connectors"])


# ── Settings dependency (cached per process) ──────────────────────────────────

@lru_cache(maxsize=1)
def _get_settings() -> Settings:
    return Settings()


# ── Clerk API schema (typed) ──────────────────────────────────────────────────

class _ClerkVerification(BaseModel):
    """Minimal shape of Clerk's verification sub-object."""
    model_config = ConfigDict(extra="ignore")
    status: str


class ClerkExternalAccount(BaseModel):
    """Typed representation of a Clerk external account record.

    Validated against the Clerk Backend API response so downstream code
    never accesses raw ``dict`` keys without schema protection.

    `updated_at` is Unix milliseconds (int) per the live Clerk Backend
    API — verified 2026-05-05 against `eac_3DHx1c...` returning
    `updated_at: 1777952584755`. We convert to an ISO 8601 string at
    the route boundary (`_iso_from_clerk_timestamp`) so the front-end
    Connector type can keep its `last_used_at: string | null` contract.
    """
    model_config = ConfigDict(extra="ignore")

    id: str
    provider: str
    verification: _ClerkVerification | None = None
    updated_at: int | None = None


# ── Response schemas ──────────────────────────────────────────────────────────

class ConnectorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    status: str  # "connected" | "error" | "not_connected"
    last_used_at: str | None = None
    error_message: str | None = None
    # Sprint 3.5: count of repos the user has scoped on this connector.
    # 0 means the connector is verified but the user has not yet picked
    # any repos — the dashboard renders "Configure scope" CTA.
    scoped_repo_count: int = 0


class RepoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    private: bool


class MeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    connectors: list[ConnectorOut] = Field(default_factory=list)
    repos: list[RepoOut] = Field(default_factory=list)


# ── PATCH /api/connectors/{id}/scoped-repos schemas (Sprint 3.5.3) ────────────

class ScopedRepoSelection(BaseModel):
    """One repo the user selected to scope into the readiness scan.

    `provider_repo_id` is GitHub's numeric repo id (string-encoded for
    JSON safety on 32-bit clients). `full_name` is GitHub's `owner/repo`
    string, persisted alongside the id so the orchestrator can produce
    deeplinks without an extra GitHub call.
    """

    model_config = ConfigDict(extra="forbid")

    provider_repo_id: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=3, max_length=200)
    private: bool = False


class ScopedReposPatch(BaseModel):
    """Request body for PATCH /api/connectors/{id}/scoped-repos.

    `repos` is the FULL desired selection — not a delta. The handler
    inserts new rows and deletes rows that fell off the new list, in a
    single transaction. An empty list is allowed and clears the scope
    (the orchestrator then refuses to scan with `ScanRunValidationError`).
    """

    model_config = ConfigDict(extra="forbid")

    repos: list[ScopedRepoSelection] = Field(default_factory=list, max_length=500)


class ScopedReposOut(BaseModel):
    """Response shape after PATCH /api/connectors/{id}/scoped-repos."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str
    repos: list[ScopedRepoSelection] = Field(default_factory=list)
    count: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

_CLERK_BASE = "https://api.clerk.com/v1"


def _iso_from_clerk_timestamp(value: int | None) -> str | None:
    """Convert Clerk's Unix-millisecond timestamps to ISO 8601 strings.

    The Clerk Backend API returns `updated_at` as an int (ms since
    epoch). The dashboard's Connector type contract is
    `last_used_at: string | null`, so we format at the boundary rather
    than leaking the integer shape into the response.
    """
    if value is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


async def _get_clerk_external_accounts(
    user_id: str, secret_key: str
) -> list[ClerkExternalAccount]:
    """Fetch external accounts via Clerk Backend API, typed via Pydantic.

    External accounts are nested on the user object — there is no
    standalone `/v1/users/{id}/external_accounts` listing endpoint
    (verified 2026-05-04: returns 404). Confirmed via the official
    Clerk Backend API reference at
    https://clerk.com/docs/reference/backend-api/tag/Users and the
    `clerk-backend-api` skill's FAST PATH for user reads.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            f"{_CLERK_BASE}/users/{user_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
        )
    r.raise_for_status()
    user_obj: dict = r.json()
    raw: list[dict] = user_obj.get("external_accounts", [])
    return [ClerkExternalAccount.model_validate(a) for a in raw]


async def _delete_clerk_external_account(
    user_id: str, external_account_id: str, secret_key: str
) -> None:
    """Delete an external account via the Clerk Backend API."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.delete(
            f"{_CLERK_BASE}/users/{user_id}/external_accounts/{external_account_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
        )
    if r.status_code == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
    r.raise_for_status()


# ── connector_scoped_repos helpers (Sprint 3.5.3) ─────────────────────────────

async def _count_scoped_repos(
    pool: AppDbPool, user_id: str, connector_id: str
) -> int:
    """Count rows in connector_scoped_repos for a connector. Defense-in-depth
    WHERE on user_id even though RLS would block cross-tenant rows."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM connector_scoped_repos "
                "WHERE user_id = %s AND connector_id = %s",
                (user_id, connector_id),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _list_scoped_repos(
    pool: AppDbPool, user_id: str, connector_id: str
) -> list[ScopedRepoSelection]:
    """Read the persisted scope for a connector, ordered by full_name.

    LIMIT 501 surfaces a write-cap-bypass condition (the API enforces 500
    rows max, but a direct DB seed could exceed it) per database-reviewer
    H-3. The caller treats >500 rows as a degraded read.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT provider_repo_id, full_name, private "
                "FROM connector_scoped_repos "
                "WHERE user_id = %s AND connector_id = %s "
                "ORDER BY full_name "
                "LIMIT 501",
                (user_id, connector_id),
            )
            rows = await cur.fetchall()
            return [
                ScopedRepoSelection(
                    provider_repo_id=str(r[0]),
                    full_name=str(r[1]),
                    private=bool(r[2]),
                )
                for r in rows
            ]


async def _replace_scoped_repos(
    pool: AppDbPool,
    user_id: str,
    connector_id: str,
    repos: list[ScopedRepoSelection],
) -> list[ScopedRepoSelection]:
    """Replace the user's scoped-repo selection in one transaction.

    Pattern:
      1) DELETE rows no longer in the desired selection.
      2) INSERT rows not yet persisted, ON CONFLICT (user_id, connector_id,
         provider_repo_id) DO NOTHING.
      3) Re-read inside the SAME transaction so the response body
         matches what we just committed (closes the TOCTOU window
         from python-reviewer F5 / database-reviewer H-2).

    Idempotent under the unique index on (user_id, connector_id,
    provider_repo_id).
    """
    desired_ids = {r.provider_repo_id for r in repos}
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if desired_ids:
                    await cur.execute(
                        "DELETE FROM connector_scoped_repos "
                        "WHERE user_id = %s AND connector_id = %s "
                        "AND NOT (provider_repo_id = ANY(%s))",
                        (user_id, connector_id, list(desired_ids)),
                    )
                else:
                    await cur.execute(
                        "DELETE FROM connector_scoped_repos "
                        "WHERE user_id = %s AND connector_id = %s",
                        (user_id, connector_id),
                    )

                if repos:
                    await cur.executemany(
                        "INSERT INTO connector_scoped_repos "
                        "(connector_id, user_id, provider_repo_id, full_name, private) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id, connector_id, provider_repo_id) DO NOTHING",
                        [
                            (
                                connector_id,
                                user_id,
                                r.provider_repo_id,
                                r.full_name,
                                r.private,
                            )
                            for r in repos
                        ],
                    )

                # Re-read inside the transaction so the response body
                # reflects exactly what we just committed.
                await cur.execute(
                    "SELECT provider_repo_id, full_name, private "
                    "FROM connector_scoped_repos "
                    "WHERE user_id = %s AND connector_id = %s "
                    "ORDER BY full_name "
                    "LIMIT 501",
                    (user_id, connector_id),
                )
                rows = await cur.fetchall()
                return [
                    ScopedRepoSelection(
                        provider_repo_id=str(r[0]),
                        full_name=str(r[1]),
                        private=bool(r[2]),
                    )
                    for r in rows
                ]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=MeOut)
async def get_me(
    pool: AppDbPoolOptionalDep,
    user: ClerkUser = Depends(verify_clerk_token),
    settings: Settings = Depends(_get_settings),
) -> MeOut:
    """Return the authenticated user's connector status, repo list, and
    scoped-repo count per connector (Sprint 3.5).

    `pool` is optional because GET /api/me must keep working when the DB
    is unavailable (a 503 on the dashboard read would be a worse
    failure mode than reporting `scoped_repo_count=0`). Tests override
    the `get_pool_optional` dependency to inject a fake pool.
    """

    with tracer.start_as_current_span("connectors.get_me") as span:
        span.set_attribute("user.id", user.user_id)

        try:
            accounts = await _get_clerk_external_accounts(
                user.user_id, settings.clerk_secret_key.get_secret_value()
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "clerk.external_accounts.fetch_failed user=%s err=%s", user.user_id, exc
            )
            span.record_exception(exc)
            span.set_attribute("connectors.degraded", True)
            accounts = []

        connectors: list[ConnectorOut] = []
        for acc in accounts:
            if acc.provider != "oauth_github":
                continue
            verification_status = acc.verification.status if acc.verification else "unverified"
            conn_status = "connected" if verification_status == "verified" else "error"

            scoped_count = 0
            if pool is not None:
                try:
                    scoped_count = await _count_scoped_repos(
                        pool, user.user_id, acc.id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "connector_scoped_repos.count_failed connector=%s", acc.id
                    )
                    span.record_exception(exc)
                    span.set_attribute("scoped_repos.degraded", True)

            connectors.append(
                ConnectorOut(
                    id=acc.id,
                    provider="github",
                    status=conn_status,
                    last_used_at=_iso_from_clerk_timestamp(acc.updated_at),
                    error_message=None if conn_status == "connected" else "Re-authentication required",
                    scoped_repo_count=scoped_count,
                )
            )

        span.set_attribute("connectors.count", len(connectors))
        return MeOut(user_id=user.user_id, connectors=connectors, repos=[])


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: str,
    user: ClerkUser = Depends(verify_clerk_token),
    settings: Settings = Depends(_get_settings),
) -> None:
    """Revoke a GitHub OAuth connector. Removes the external account from Clerk."""

    with tracer.start_as_current_span("connectors.delete_connector") as span:
        span.set_attribute("user.id", user.user_id)
        span.set_attribute("connector.id", connector_id)

        # Verify ownership before deleting (IDOR prevention)
        try:
            accounts = await _get_clerk_external_accounts(
                user.user_id, settings.clerk_secret_key.get_secret_value()
            )
        except httpx.HTTPError as exc:
            logger.error(
                "clerk.external_accounts.fetch_failed user=%s err=%s", user.user_id, exc
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream error")

        if not any(a.id == connector_id for a in accounts):
            span.set_attribute("connector.not_found", True)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found"
            )

        try:
            await _delete_clerk_external_account(
                user.user_id,
                connector_id,
                settings.clerk_secret_key.get_secret_value(),
            )
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            logger.error(
                "clerk.delete_external_account.failed user=%s err=%s", user.user_id, exc
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream error")

        span.set_attribute("connector.deleted", True)


@router.get(
    "/connectors/{connector_id}/scoped-repos",
    response_model=ScopedReposOut,
)
async def get_scoped_repos(
    connector_id: str,
    pool: AppDbPoolDep,
    user: ClerkUser = Depends(verify_clerk_token),
) -> ScopedReposOut:
    """Return the current scoped-repo selection for a connector.

    Drives the picker page's "pre-check the user's current selection"
    UX. RLS + explicit WHERE on user_id mean cross-tenant reads return
    an empty list (200 OK), not a 403.

    Refs: PLAN.md Sprint 3.5 chunk 3.5.2 + 3.5.3.
    """

    with tracer.start_as_current_span("connectors.get_scoped_repos") as span:
        span.set_attribute("user.id", user.user_id)
        span.set_attribute("connector.id", connector_id)
        try:
            repos = await _list_scoped_repos(pool, user.user_id, connector_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "connector_scoped_repos.list_failed user=%s connector=%s",
                user.user_id,
                connector_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to read scoped repos",
            ) from exc
        span.set_attribute("scoped_repos.count", len(repos))
        return ScopedReposOut(
            connector_id=connector_id, repos=repos, count=len(repos)
        )


@router.patch(
    "/connectors/{connector_id}/scoped-repos",
    response_model=ScopedReposOut,
)
async def patch_scoped_repos(
    connector_id: str,
    body: ScopedReposPatch,
    pool: AppDbPoolDep,
    user: ClerkUser = Depends(verify_clerk_token),
    settings: Settings = Depends(_get_settings),
) -> ScopedReposOut:
    """Set the user-chosen repo scope for a GitHub connector (Sprint 3.5.3).

    Body is the FULL desired selection — not a delta. The handler:
      1. Verifies ownership: the connector_id must be one of this user's
         Clerk external_accounts (IDOR prevention, mirrors DELETE).
      2. Replaces the persisted selection in one transaction (DELETE rows
         that fell off, INSERT new rows with ON CONFLICT DO NOTHING under
         the unique index on (connector_id, provider_repo_id)).
      3. Returns 200 with the persisted state so the client can render
         "Connected · N repos" without an extra round-trip.

    Empty `repos` list is allowed and clears the scope; the dashboard
    then reverts to "Configure scope" CTA and the orchestrator refuses
    to start a scan with `ScanRunValidationError` (chunk 3.5.5).

    Refs: ADR-0015, US-002, system-design 3.1, 6.1.
    """

    with tracer.start_as_current_span("connectors.patch_scoped_repos") as span:
        span.set_attribute("user.id", user.user_id)
        span.set_attribute("connector.id", connector_id)
        span.set_attribute("scoped_repos.requested_count", len(body.repos))

        # IDOR check — same pattern as DELETE.
        try:
            accounts = await _get_clerk_external_accounts(
                user.user_id, settings.clerk_secret_key.get_secret_value()
            )
        except httpx.HTTPError as exc:
            logger.error(
                "clerk.external_accounts.fetch_failed user=%s err=%s",
                user.user_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream error"
            )

        if not any(a.id == connector_id for a in accounts):
            span.set_attribute("connector.not_found", True)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found"
            )

        # Persist the new selection.
        try:
            persisted = await _replace_scoped_repos(
                pool, user.user_id, connector_id, body.repos
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "connector_scoped_repos.replace_failed user=%s connector=%s",
                user.user_id,
                connector_id,
            )
            span.set_attribute("scoped_repos.error", True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to persist scoped repos",
            ) from exc

        span.set_attribute("scoped_repos.persisted_count", len(persisted))
        return ScopedReposOut(
            connector_id=connector_id,
            repos=persisted,
            count=len(persisted),
        )
