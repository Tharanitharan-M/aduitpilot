"""Sprint 9 chunks 9.10, 9.11, 9.12 — scan_runs route tests.

Covers:
  - GET /api/scan-runs lists the user's runs (cross-tenant excluded)
  - GET /api/scan-runs/{id} returns the row, 404s on miss
  - POST /api/scan-runs body={"source":"rerun","parent":...} creates a
    new row whose parent_scan_run_id points at the original
  - POST with unknown parent returns 404
  - GET /api/scan-runs/diff?a=&b= returns 422 when missing params
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.auth.clerk import ClerkUser

FAKE_USER = ClerkUser(user_id="user_abc", session_id="sess_123")


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "postgres://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_fake")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")

    import apps.api.main as main_module

    main_module = importlib.reload(main_module)
    from apps.api.auth.clerk import verify_clerk_token

    main_module.app.dependency_overrides[verify_clerk_token] = lambda: FAKE_USER
    return TestClient(main_module.app)


# ── Fake pool ────────────────────────────────────────────────────────────────


class _Cur:
    def __init__(self, store: dict) -> None:
        self._s = store
        self._rows: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())
        if "set_config" in q:
            return

        if q.startswith("SELECT id, user_id, connector_id, repo_include_list"):
            user_id = params[0]
            if "id::text = %s" in q or "id = %s::uuid" in q:
                _, run_id = params
                hit = [
                    r
                    for r in self._s["runs"]
                    if r["user_id"] == user_id and str(r["id"]) == run_id
                ]
                self._rows = [_run_row(hit[0])] if hit else []
            else:
                hits = [r for r in self._s["runs"] if r["user_id"] == user_id]
                hits.sort(key=lambda r: r["started_at"], reverse=True)
                self._rows = [_run_row(r) for r in hits]
            return

        if q.startswith("SELECT connector_id FROM scan_runs"):
            user_id, run_id = params
            hit = [r for r in self._s["runs"] if r["user_id"] == user_id and str(r["id"]) == run_id]
            self._rows = [(hit[0]["connector_id"],)] if hit else []
            return

        if q.startswith("SELECT provider_repo_id FROM connector_scoped_repos"):
            user_id, connector_id = params
            self._rows = [
                (r["provider_repo_id"],)
                for r in self._s["scoped_repos"]
                if r["user_id"] == user_id and r["connector_id"] == connector_id
            ]
            return

        if q.startswith("INSERT INTO scan_runs"):
            user_id, connector_id, repo_list, parent_id = params
            new_id = uuid.uuid4()
            self._s["runs"].append(
                {
                    "id": new_id,
                    "user_id": user_id,
                    "connector_id": connector_id,
                    "repo_include_list": list(repo_list or []),
                    "status": "running",
                    "started_at": datetime.now(UTC),
                    "completed_at": None,
                    "cancelled": False,
                    "parent_scan_run_id": uuid.UUID(parent_id) if parent_id else None,
                }
            )
            self._rows = [(new_id,)]
            return

        # diff: FULL OUTER JOIN result.
        if "FULL OUTER JOIN cmap_b" in q:
            self._rows = self._s.get("diff_rows", [])
            return

        if q.startswith("SELECT id::text FROM evidence WHERE user_id = %s AND scan_run_id"):
            user_id, scan_run_id = params
            self._rows = [
                (eid,) for (uid, sid, eid) in self._s["evidence_links"]
                if uid == user_id and sid == scan_run_id
            ]
            return

        raise AssertionError(f"unexpected SQL: {q!r}")

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


def _run_row(r: dict) -> tuple:
    return (
        r["id"],
        r["user_id"],
        r.get("connector_id"),
        r.get("repo_include_list", []),
        r.get("status", "running"),
        r.get("started_at"),
        r.get("completed_at"),
        r.get("cancelled", False),
        r.get("parent_scan_run_id"),
    )


class _Conn:
    def __init__(self, store: dict) -> None:
        self._s = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._s)

    def transaction(self):
        return self

    async def execute(self, query: str, params: tuple = ()) -> Any:
        cur = _Cur(self._s)
        await cur.execute(query, params)
        return cur


class _Pool:
    def __init__(self) -> None:
        self.store: dict = {
            "runs": [],
            "scoped_repos": [],
            "evidence_links": [],
            "diff_rows": [],
        }

    def connection(self):
        return _Conn(self.store)

    def seed_run(self, **fields) -> str:
        run_id = fields.pop("id", uuid.uuid4())
        record = {
            "id": run_id,
            "user_id": fields.pop("user_id", FAKE_USER.user_id),
            "connector_id": fields.pop("connector_id", "github_default"),
            "repo_include_list": fields.pop("repo_include_list", ["1234"]),
            "status": fields.pop("status", "completed"),
            "started_at": fields.pop("started_at", datetime.now(UTC)),
            "completed_at": fields.pop("completed_at", datetime.now(UTC)),
            "cancelled": fields.pop("cancelled", False),
            "parent_scan_run_id": fields.pop("parent_scan_run_id", None),
        }
        self.store["runs"].append(record)
        return str(run_id)


# ── Tests ───────────────────────────────────────────────────────────────────


def test_list_runs(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()
    pool.seed_run()
    pool.seed_run(user_id="user_other")

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/scan-runs", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["runs"][0]["user_id"] == FAKE_USER.user_id


def test_get_run_404(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get(
        f"/api/scan-runs/{uuid.uuid4()}", headers={"Authorization": "Bearer fake"}
    )
    assert r.status_code == 404


def test_create_rerun_links_parent(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()
    parent_id = pool.seed_run()
    # The user has one repo in scope.
    pool.store["scoped_repos"].append(
        {
            "user_id": FAKE_USER.user_id,
            "connector_id": "github_default",
            "provider_repo_id": "9876",
        }
    )

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.post(
        "/api/scan-runs",
        json={"source": "rerun", "parent": parent_id, "params_override": {}},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parent_scan_run_id"] == parent_id
    assert body["status"] == "running"
    # The new run inherited the user's CURRENT scope, not the parent's.
    assert body["repo_include_list"] == ["9876"]


def test_create_rerun_unknown_parent_returns_404(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.post(
        "/api/scan-runs",
        json={"source": "rerun", "parent": str(uuid.uuid4()), "params_override": {}},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 404


def test_diff_runs_requires_both_query_params(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/scan-runs/diff", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 422


def test_diff_runs_returns_typed_diff(monkeypatch):
    client = _client(monkeypatch)
    pool = _Pool()
    a_id = pool.seed_run()
    b_id = pool.seed_run()
    pool.store["diff_rows"] = [
        ("CC6.1", "passing", "failing", 0.85, 0.30, "rA", "rB"),
        ("CC7.1", "passing", "passing", 0.90, 0.90, "same", "same"),
    ]
    pool.store["evidence_links"] = [
        (FAKE_USER.user_id, a_id, "ev-1"),
        (FAKE_USER.user_id, b_id, "ev-1"),
        (FAKE_USER.user_id, b_id, "ev-2"),
    ]

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get(
        f"/api/scan-runs/diff?a={a_id}&b={b_id}",
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["a"] == a_id
    assert body["b"] == b_id
    # Only the regressed control should appear (the unchanged one was filtered).
    assert len(body["controls_changed"]) == 1
    assert body["controls_changed"][0]["control_id"] == "CC6.1"
    assert body["controls_changed"][0]["a_status"] == "passing"
    assert body["controls_changed"][0]["b_status"] == "failing"
    assert body["evidence_added"] == ["ev-2"]
    assert body["evidence_removed"] == []
