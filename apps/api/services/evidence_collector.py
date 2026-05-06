"""
Per-repo evidence collection contract
=====================================
Sprint 4 chunk 4.4b: defines the coroutine signature the
``collect_evidence`` graph node calls. Sprint 4 ships only a
deterministic stub that returns a single placeholder ``Evidence`` row
per repo so the graph integration tests can run without spawning a real
GitHub MCP subprocess. Sprint 5 (chunks 5.3 - 5.7) replaces the stub with
parallel calls to GitHub MCP for branch protection, MFA, code scanning,
secret scanning, and Dependabot.

Why a callable contract instead of a class?
------------------------------------------
Callable indirection makes test injection trivial — graph tests pass
a lambda; production passes the real coroutine — and avoids forcing
every Sprint-5 evidence type into a single class hierarchy. Each Sprint-5
chunk adds its own coroutine and they all conform to the same shape.

Refs: PLAN.md Sprint 4 chunk 4.4b; ADR-0004 (read-only); ADR-0015
(repo-scoped reads); system-design.md 3.2, 6.6.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from apps.api.state import Evidence

# A coroutine that, given a repo id and an optional scan_run id, returns
# zero or more Evidence rows for that repo. The graph node fans these
# out via ``asyncio.gather(*tasks, return_exceptions=True)`` so per-repo
# failures isolate. PEP 695 ``type`` syntax (Python 3.12+).
type EvidenceCollector = Callable[
    ...,  # keyword-only: repo_id=..., scan_run_id=...
    Awaitable[list[Evidence]],
]


async def default_evidence_collector(
    *,
    repo_id: str,
    scan_run_id: str | None = None,
) -> list[Evidence]:
    """Sprint 4 stub — emit one placeholder Evidence row per scoped repo.

    The row records ``source_type='mock'`` and includes the repo id +
    scan_run id in ``raw`` so downstream nodes can verify the scope was
    honoured (the chunk 4.4b auto-test reads this to assert the
    orchestrator state has evidence rows whose source_uri matches the
    scoped repo set).

    Sprint 5 replaces this with the real GitHub MCP fetches; the
    function signature stays the same so the graph node does not change.
    """

    canonical = json.dumps(
        {"repo_id": repo_id, "scan_run_id": scan_run_id, "kind": "stub"},
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence = Evidence(
        id=f"ev_stub_{content_hash[:16]}",
        source_type="mock",
        source_uri=f"github://{repo_id}",
        raw={
            "repo_id": repo_id,
            "scan_run_id": scan_run_id,
            "kind": "stub",
            "note": (
                "Sprint 4 placeholder. Sprint 5 chunk 5.3+ replaces with real "
                "GitHub MCP evidence (branch protection, MFA, code scanning, "
                "secret scanning, Dependabot)."
            ),
        },
        content_hash=content_hash,
        collected_at=datetime.now(UTC),
    )
    return [evidence]


__all__ = [
    "EvidenceCollector",
    "default_evidence_collector",
]
