"""Sprint 8 chunks 8.5 / 8.6 / 8.7 — /api/mock-audit FastAPI surface.

Uses an in-memory pool stub mirroring test_questionnaire.py so the route
contract can be exercised without a live Postgres.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.auth.clerk import ClerkUser, verify_clerk_token

# ── In-memory pool stub mirroring test_questionnaire.py ──────────────────────


class _Cursor:
    def __init__(self, store: _MockAuditStore, user_id: str) -> None:
        self._store = store
        self._user_id = user_id
        self._result: list[tuple] = []
        self._single: tuple | None = None

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())
        if q.startswith("SELECT id::text FROM mock_audit_runs WHERE user_id"):
            user_id, key = params
            run = self._store.find_by_key(user_id, key)
            self._single = (run["id"],) if run else None
            return
        if q.startswith("SELECT id::text, user_id, scan_run_id::text, status, summary"):
            if "id = %s::uuid" in q:
                user_id, run_id = params
                row = self._store.runs.get((user_id, run_id))
                self._single = self._store.row(row) if row else None
            else:
                user_id = params[0]
                self._result = [
                    self._store.row(r)
                    for (uid, _), r in self._store.runs.items()
                    if uid == user_id
                ]
            return
        if q.startswith("SELECT id::text, run_id::text, severity, tsc_id, objection"):
            user_id, run_id = params
            self._result = [
                self._store.finding_row(f)
                for f in self._store.findings_for(user_id, run_id)
            ]
            return
        # Fallback – store nothing
        self._result = []
        self._single = None

    async def fetchone(self) -> tuple | None:
        return self._single

    async def fetchall(self) -> list[tuple]:
        return self._result


class _Conn:
    def __init__(self, store: _MockAuditStore) -> None:
        self._store = store
        self._user_id = ""

    @asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())
        if q.startswith("SELECT set_config('app.current_user_id'"):
            self._user_id = params[0]
            return
        if q.startswith("INSERT INTO mock_audit_runs"):
            run_id, user_id, scan_run_id, cap_usd, key = params
            self._store.runs[(user_id, run_id)] = {
                "id": run_id,
                "user_id": user_id,
                "scan_run_id": scan_run_id,
                "status": "queued",
                "summary": "",
                "findings_count": 0,
                "severity_max": "none",
                "spent_usd": 0.0,
                "cap_usd": float(cap_usd),
                "a2a_task_id": None,
                "report_r2_key": None,
                "failure_reason": None,
                "job_idempotency_key": key,
                "created_at": "2026-05-08T12:00:00+00:00",
                "updated_at": "2026-05-08T12:00:00+00:00",
            }
            return

    async def commit(self) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self._store, self._user_id)


class _PoolCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, store: _MockAuditStore) -> None:
        self._store = store

    def connection(self) -> _PoolCM:
        return _PoolCM(_Conn(self._store))


class _MockAuditStore:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], dict] = {}
        self.findings: dict[tuple[str, str], list[dict]] = {}

    def find_by_key(self, user_id: str, key: str) -> dict | None:
        for (uid, _), r in self.runs.items():
            if uid == user_id and r["job_idempotency_key"] == key and r["status"] not in {
                "failed",
                "completed",
                "budget_exceeded",
            }:
                return r
        return None

    def findings_for(self, user_id: str, run_id: str) -> list[dict]:
        return [
            f for f in self.findings.get((user_id, run_id), [])
        ]

    @staticmethod
    def row(r: dict) -> tuple:
        return (
            r["id"],
            r["user_id"],
            r["scan_run_id"],
            r["status"],
            r["summary"],
            r["findings_count"],
            r["severity_max"],
            r["spent_usd"],
            r["cap_usd"],
            r["a2a_task_id"],
            r["report_r2_key"],
            r["failure_reason"],
            r["created_at"],
            r["updated_at"],
        )

    @staticmethod
    def finding_row(f: dict) -> tuple:
        return (
            f["id"],
            f["run_id"],
            f["severity"],
            f.get("tsc_id"),
            f["objection"],
            f.get("recommended_next_step", ""),
            f.get("sequence_idx", 0),
        )


# ── Fixtures ────────────────────────────────────────────────────────────────


class _StubQueue:
    def __init__(self) -> None:
        self.enqueued: list[Any] = []

    async def enqueue(self, message):  # type: ignore[no-untyped-def]
        self.enqueued.append(message)

        class _Result:
            message_id = "queued-1"
            deduplicated = False

        return _Result()


@pytest.fixture
def mock_audit_app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MOCK_AUDIT_RATE_LIMIT", "1000/minute")
    from apps.api.db import get_pool
    from apps.api.main import app
    from apps.api.routes.mock_audit import _get_job_queue, _get_storage

    store = _MockAuditStore()
    pool = _Pool(store)
    queue = _StubQueue()

    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[verify_clerk_token] = lambda: ClerkUser(
        user_id="user_test",
        session_id="sess_test",
    )
    app.dependency_overrides[_get_job_queue] = lambda: queue
    # storage is only used by the report route which we don't exercise here
    app.dependency_overrides[_get_storage] = lambda: object()

    # Do NOT enter the TestClient as a context manager — that would trigger
    # the FastAPI lifespan, which tries to open real Redis + Postgres
    # connections and times out in CI without those services. The questionnaire
    # tests follow the same pattern.
    client = TestClient(app)
    try:
        yield client, store, queue
    finally:
        app.dependency_overrides.clear()


def test_start_run_enqueues_and_returns_run_id(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, store, queue = mock_audit_app
    response = client.post("/api/mock-audit/run", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["deduplicated"] is False
    assert len(queue.enqueued) == 1
    msg = queue.enqueued[0]
    assert msg.type == "mock_audit.run"
    # Run row exists.
    assert any(r["id"] == body["run_id"] for r in store.runs.values())


def test_start_run_dedups_on_repeat(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, _store, _queue = mock_audit_app
    a = client.post("/api/mock-audit/run", json={}).json()
    b = client.post("/api/mock-audit/run", json={}).json()
    assert a["run_id"] == b["run_id"]
    assert b["deduplicated"] is True


def test_list_runs_returns_inserted_row(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, _store, _queue = mock_audit_app
    client.post("/api/mock-audit/run", json={})
    response = client.get("/api/mock-audit")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["runs"][0]["status"] == "queued"


def test_get_run_returns_findings_empty_initially(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, _store, _queue = mock_audit_app
    run_id = client.post("/api/mock-audit/run", json={}).json()["run_id"]
    response = client.get(f"/api/mock-audit/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == run_id
    assert body["findings"] == []


def test_get_run_404_for_unknown_id(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, _store, _queue = mock_audit_app
    bogus = uuid.uuid4().hex
    response = client.get(f"/api/mock-audit/{bogus}")
    assert response.status_code == 404


def test_poll_run_404_for_unknown_id(mock_audit_app) -> None:  # type: ignore[no-untyped-def]
    client, _store, _queue = mock_audit_app
    response = client.get(f"/api/mock-audit/{uuid.uuid4().hex}/poll")
    assert response.status_code == 404
