"""Sprint 8 chunk 8.5 / 8.7 — MockAuditRunHandler unit tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from apps.api.agents.remote_auditor import A2ATaskResult
from apps.api.jobs import JobMessage, JobType
from apps.api.services.mock_audit_worker import (
    MockAuditRunHandler,
    _max_severity,
)


def test_max_severity_picks_highest() -> None:
    findings = [
        {"severity": "low"},
        {"severity": "critical"},
        {"severity": "medium"},
    ]
    assert _max_severity(findings) == "critical"


def test_max_severity_no_findings_is_none() -> None:
    assert _max_severity([]) == "none"


# ── In-memory storage + fake DB pool helpers ─────────────────────────────────


class _RecordingStorage:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, str]] = []

    def make_key(self, *, user_id: str, kind: str, suffix: str = "") -> str:
        return f"users/{user_id}/{kind}/abc{suffix}"

    def put_bytes(self, key: str, body: bytes, *, content_type: str):  # type: ignore[no-untyped-def]
        self.put_calls.append((key, body, content_type))

        class _Stored:
            backend = "local"
            size_bytes = len(body)

        _Stored.key = key  # type: ignore[attr-defined]
        return _Stored()


class _FakePool:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.user_id = ""

    @asynccontextmanager
    async def connection(self):  # type: ignore[no-untyped-def]
        yield self

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def execute(self, query: str, params: tuple = ()) -> None:
        self.statements.append((query.strip().split("\n")[0], params))

    async def commit(self) -> None:
        return None

    def cursor(self):  # type: ignore[no-untyped-def]
        pool = self

        class _Cur:
            async def __aenter__(self) -> _Cur:
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def execute(self, q: str, p: tuple = ()) -> None:
                pool.statements.append((q.strip().split("\n")[0], p))

            async def fetchone(self) -> Any:
                return None

            async def fetchall(self) -> list[tuple]:
                return []

        return _Cur()


class _StubAgent:
    """Stand-in for RemoteA2aAgent that returns a canned A2ATaskResult."""

    def __init__(self, *, result: A2ATaskResult) -> None:
        self._result = result
        self.send_calls = 0
        self.closed = False

    async def send_message(self, scan_context: dict[str, Any]) -> A2ATaskResult:
        self.send_calls += 1
        return self._result

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_handler_completed_run_persists_findings_and_uploads_report() -> None:
    storage = _RecordingStorage()
    pool = _FakePool()
    completed = A2ATaskResult(
        task_id="task-1",
        state="TASK_STATE_COMPLETED",
        findings=[
            {"severity": "high", "tsc_id": "CC6.1", "objection": "x", "recommended_next_step": "y"},
            {"severity": "low", "tsc_id": None, "objection": "y", "recommended_next_step": ""},
        ],
        summary="ok",
        budget={"spent_usd": 0.01, "cap_usd": 0.5, "calls": 1},
    )
    agent = _StubAgent(result=completed)

    async def _factory():  # type: ignore[no-untyped-def]
        return agent

    async def _loader(user_id: str, scan_run_id: str) -> dict[str, Any]:
        return {"control_map": [], "evidence": []}

    handler = MockAuditRunHandler(
        pool_factory=lambda: pool,
        storage=storage,  # type: ignore[arg-type]
        remote_agent_factory=_factory,
        scan_context_loader=_loader,
    )
    message = JobMessage(
        type=JobType.MOCK_AUDIT_RUN,
        user_id="user_x",
        idempotency_key="k",
        payload={"run_id": "11111111-1111-1111-1111-111111111111"},
    )
    await handler(message)

    # The agent was invoked and closed.
    assert agent.send_calls == 1
    assert agent.closed
    # The Markdown gap report was uploaded.
    assert len(storage.put_calls) == 1
    assert storage.put_calls[0][2] == "text/markdown"
    body = storage.put_calls[0][1].decode("utf-8")
    assert "Mock readiness challenge" in body
    # Status moved through: dispatching, running, terminal completed UPDATE
    statements = " ".join(s[0] for s in pool.statements)
    assert "UPDATE mock_audit_runs" in statements


@pytest.mark.asyncio
async def test_handler_budget_exceeded_marks_terminal() -> None:
    storage = _RecordingStorage()
    pool = _FakePool()
    result = A2ATaskResult(
        task_id="task-2",
        state="TASK_STATE_BUDGET_EXCEEDED",
        findings=[],
        summary="",
        budget={"spent_usd": 0.51, "cap_usd": 0.5, "calls": 4},
        error="budget exceeded",
    )
    agent = _StubAgent(result=result)

    async def _factory():  # type: ignore[no-untyped-def]
        return agent

    async def _loader(user_id: str, scan_run_id: str) -> dict[str, Any]:
        return {"control_map": []}

    handler = MockAuditRunHandler(
        pool_factory=lambda: pool,
        storage=storage,  # type: ignore[arg-type]
        remote_agent_factory=_factory,
        scan_context_loader=_loader,
    )
    await handler(
        JobMessage(
            type=JobType.MOCK_AUDIT_RUN,
            user_id="user_x",
            idempotency_key="k2",
            payload={"run_id": "22222222-2222-2222-2222-222222222222"},
        )
    )
    # The terminal UPDATE carried 'budget_exceeded'.
    flat = [str(p) for p in pool.statements if p[1]]
    assert any("budget_exceeded" in str(s) for s in flat) or any(
        "budget_exceeded" in str(s) for s in pool.statements
    )
