"""psycopg AsyncConnectionPool factory + FastAPI dependency.

The application's own DB pool (separate from LangGraph's PostgresSaver) is
owned by the FastAPI lifespan in ``apps.api.main``. Routes that need a
connection inject :data:`AppDbPoolDep` and call ``async with pool.connection()``
inside the handler. Tests override the dependency via
``app.dependency_overrides[get_pool] = lambda: fake_pool`` so no real
Postgres is required for unit tests.

The pool is lazy: ``init_pool()`` is a no-op when ``database_url`` is not
configured, returning ``None`` and letting routes that depend on it fail
fast with a 503 instead of crashing the whole app at startup.

Refs: PLAN.md Sprint 3.5 chunk 3.5.3; ADR-0008; system-design 4.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.config import Settings

logger = logging.getLogger(__name__)

# Module-level pool. The lifespan owns this; tests override via
# ``app.dependency_overrides`` rather than touching the singleton.
_pool: AsyncConnectionPool | None = None

# Public type alias for route signatures.
AppDbPool = AsyncConnectionPool


async def init_pool(
    settings: "Settings",
    *,
    min_size: int = 1,
    max_size: int = 10,
    acquire_timeout: float = 10.0,
    max_waiting: int = 50,
) -> AppDbPool | None:
    """Open the application DB pool (idempotent).

    Returns ``None`` when ``database_url`` is unset so the rest of the
    lifespan still boots — useful for the dev path where the app boots
    without a configured Postgres for the moment.

    `acquire_timeout` (default 10 s, per python-reviewer F8) bounds how
    long a request may wait to check out a connection before failing
    fast — preferable to the psycopg-pool default of 300 s, which would
    starve FastAPI worker slots under a Postgres outage. `max_waiting`
    (50) caps the queue of waiters so we never accumulate an unbounded
    backlog.
    """

    global _pool
    if _pool is not None:
        return _pool

    if settings.database_url is None:
        logger.info("db.pool.skipped reason=no_database_url")
        return None

    url = settings.database_url.get_secret_value()
    pool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        timeout=acquire_timeout,
        max_waiting=max_waiting,
        # ``open=False`` + explicit ``open(wait=True)`` below gives us a
        # single deterministic point at which the pool reaches READY.
        # ``wait=True`` blocks the lifespan briefly until at least one
        # connection negotiation has succeeded, so the first request
        # after boot does not race a half-warm pool (python-reviewer F1).
        open=False,
    )
    try:
        await pool.open(wait=True, timeout=acquire_timeout)
    except Exception:  # noqa: BLE001
        logger.exception(
            "db.pool.open_failed — DB-backed routes will return 503 until reachable"
        )
        await pool.close()
        return None
    _pool = pool
    logger.info(
        "db.pool.opened min_size=%d max_size=%d timeout=%.1fs",
        min_size,
        max_size,
        acquire_timeout,
    )
    return _pool


async def close_pool() -> None:
    """Close the application DB pool. Safe to call when no pool exists."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception:  # noqa: BLE001
        logger.exception("db.pool.close_failed")
    _pool = None


def get_pool() -> AppDbPool:
    """FastAPI dependency: return the live pool or raise 503.

    The 503 path covers the ``database_url is None`` boot path — a route
    that needs the pool fails with a friendly Service Unavailable rather
    than an unhelpful AttributeError.
    """
    if _pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )
    return _pool


def get_pool_optional() -> AppDbPool | None:
    """Best-effort variant of :func:`get_pool` for routes that can degrade
    gracefully when the DB is unavailable (e.g. GET /api/me's
    scoped_repo_count read). Returns ``None`` instead of raising 503 so
    the caller can render a sensible default."""
    return _pool


# Route-side annotations. Use ``pool: AppDbPoolDep`` for required-pool
# routes and ``pool: AppDbPoolOptionalDep`` for optional-pool reads.
AppDbPoolDep = Annotated[AppDbPool, Depends(get_pool)]
AppDbPoolOptionalDep = Annotated[AppDbPool | None, Depends(get_pool_optional)]


__all__ = [
    "AppDbPool",
    "AppDbPoolDep",
    "AppDbPoolOptionalDep",
    "close_pool",
    "get_pool",
    "get_pool_optional",
    "init_pool",
]
