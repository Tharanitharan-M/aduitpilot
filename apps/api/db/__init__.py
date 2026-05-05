"""Database access for the FastAPI app.

The LangGraph `AsyncPostgresSaver` (chunk 2.6) owns its own connection
pool internally and does not expose it. The application code (Sprint 3.5
onward, chunk 3.5.3) needs its own pool for non-checkpoint reads/writes
— starting with the `connector_scoped_repos` PATCH endpoint.

Refs: PLAN.md Sprint 3.5 chunk 3.5.3, ADR-0008, system-design 4.
"""

from apps.api.db.pool import (
    AppDbPool,
    AppDbPoolDep,
    AppDbPoolOptionalDep,
    close_pool,
    get_pool,
    get_pool_optional,
    init_pool,
)

__all__ = [
    "AppDbPool",
    "AppDbPoolDep",
    "AppDbPoolOptionalDep",
    "close_pool",
    "get_pool",
    "get_pool_optional",
    "init_pool",
]
