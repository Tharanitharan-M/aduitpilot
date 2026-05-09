"""Sprint 9 chunks 9.6, 9.8, 9.9 — drift route tests.

Covers:
  - POST /api/drift/run with cron token enqueues per-user jobs
  - POST /api/drift/run with bad cron token returns 401
  - POST /api/drift/run with JWT enqueues exactly one job for the caller
  - GET /api/drift/events lists the caller's events
  - PATCH /api/drift/events/{id} dismiss flips state and 404s on miss
  - PATCH dismiss without reason returns 422
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
OTHER_USER = ClerkUser(user_id="user_xyz", session_id="sess_456")


def _make_client(monkeypatch: pytest.MonkeyPatch, user: ClerkUser = FAKE_USER) -> TestClient:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "postgres://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_fake")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")

    import apps.api.main as main_module

    main_module = importlib.reload(main_module)
    from apps.api.auth.clerk import verify_clerk_token
    from apps.api.routes.drift import _optional_clerk_user

    main_module.app.dependency_overrides[verify_clerk_token] = lambda: user
    # Cron path: tests that pass X-Cron-Token expect this override to
    # behave as the production code does (return None unless an auth
    # header is set). The cron route discriminates on the header itself.
    main_module.app.dependency_overrides[_optional_clerk_user] = lambda: user
    return TestClient(main_module.app)


# ── Fake pool — drift events store ──────────────────────────────────────────


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

        if q.startswith("SELECT id::text, user_id, control_id, event_type"):
            user_id = params[0]
            results: list[tuple] = []
            for ev in self._s["events"]:
                if ev["user_id"] != user_id:
                    continue
                if "AND status = %s" in q and ev["status"] != params[1]:
                    continue
                results.append(_event_row(ev))
            results.sort(key=lambda r: r[10], reverse=True)
            self._rows = results
            return

        if q.startswith("UPDATE drift_events SET status = 'dismissed'"):
            reason, user_id, event_id = params
            for ev in self._s["events"]:
                if (
                    ev["user_id"] == user_id
                    and ev["id"] == event_id
                    and ev["status"] == "open"
                ):
                    ev["status"] = "dismissed"
                    ev["dismissed_reason"] = reason
                    self._rows = [_event_row(ev)]
                    return
            self._rows = []
            return

        if q.startswith("UPDATE drift_events SET status = 'resolved'"):
            user_id, event_id = params
            for ev in self._s["events"]:
                if (
                    ev["user_id"] == user_id
                    and ev["id"] == event_id
                    and ev["status"] == "open"
                ):
                    ev["status"] = "resolved"
                    self._rows = [_event_row(ev)]
                    return
            self._rows = []
            return

        if q.startswith("SELECT control_id FROM monitored_controls"):
            user_id = params[0]
            self._rows = [
                (cid,)
                for (uid, cid), monitored in self._s["monitored"].items()
                if uid == user_id and monitored is False
            ]
            return

        if q.startswith("INSERT INTO monitored_controls"):
            user_id, control_id, monitored = params
            self._s["monitored"][(user_id, control_id)] = bool(monitored)
            return

        if q.startswith("SELECT DISTINCT user_id FROM connector_scoped_repos"):
            self._rows = [(uid,) for uid in self._s["active_users"]]
            return

        if q.startswith("SELECT count(*)::int, max(last_seen_at)"):
            user_id = params[0]
            snaps = self._s.get("snapshots", {})
            count = sum(1 for (uid, _cid) in snaps if uid == user_id)
            last = max(
                (data.get("last_seen_at") for (uid, _), data in snaps.items() if uid == user_id),
                default=None,
            )
            self._rows = [(count, last)]
            return

        if q.startswith("SELECT count(*)::int AS total, count(*) FILTER"):
            user_id = params[0]
            evs = [ev for ev in self._s["events"] if ev["user_id"] == user_id]
            total = len(evs)
            open_count = sum(1 for ev in evs if ev["status"] == "open")
            self._rows = [(total, open_count)]
            return

        raise AssertionError(f"unexpected SQL: {q!r}")

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


def _event_row(ev: dict) -> tuple:
    return (
        ev["id"],
        ev["user_id"],
        ev["control_id"],
        ev["event_type"],
        ev.get("what_changed", ""),
        ev.get("previous_value", {}),
        ev.get("current_value", {}),
        ev.get("suggested_fix", ""),
        ev.get("source_link"),
        ev.get("severity", "medium"),
        ev.get("detected_at", datetime.now(UTC)),
        ev.get("status", "open"),
        ev.get("content_hash", ""),
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
            "events": [],
            "monitored": {},
            "active_users": [FAKE_USER.user_id, OTHER_USER.user_id],
            "enqueued": [],
        }

    def connection(self):
        return _Conn(self.store)


class _FakeQueue:
    def __init__(self, store: dict) -> None:
        self._s = store

    async def enqueue(self, message) -> Any:
        # ``message.type`` may have been coerced to a plain str by
        # JobMessage's ``use_enum_values=True`` config; handle either.
        type_str = message.type.value if hasattr(message.type, "value") else str(message.type)
        self._s["enqueued"].append((type_str, message.user_id))

        class _R:
            message_id = "fake-id"
            deduplicated = False

        return _R()


def _bind_queue(main_module, store: dict) -> None:
    from apps.api.routes import drift as drift_route_mod

    queue = _FakeQueue(store)
    main_module.app.dependency_overrides[drift_route_mod._get_job_queue] = (
        lambda: queue
    )


def _seed_event(pool: _Pool, **fields) -> str:
    eid = fields.pop("id", str(uuid.uuid4()))
    now = datetime.now(UTC)
    record = {
        "id": eid,
        "user_id": fields.pop("user_id", FAKE_USER.user_id),
        "control_id": fields.pop("control_id", "CC6.1"),
        "event_type": fields.pop("event_type", "config_changed"),
        "what_changed": fields.pop("what_changed", "Branch protection disabled"),
        "previous_value": fields.pop("previous_value", {"enforcement": "active"}),
        "current_value": fields.pop("current_value", {"enforcement": "disabled"}),
        "suggested_fix": fields.pop("suggested_fix", ""),
        "source_link": fields.pop("source_link", None),
        "severity": fields.pop("severity", "high"),
        "status": fields.pop("status", "open"),
        "content_hash": fields.pop(
            "content_hash", "deadbeef" * 8
        ),
        "detected_at": fields.pop("detected_at", now),
    }
    if fields:
        raise AssertionError(f"unexpected seed fields: {fields}")
    pool.store["events"].append(record)
    return eid


# ── Tests ───────────────────────────────────────────────────────────────────


def test_run_drift_with_cron_token(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool
    _bind_queue(main_module, pool.store)

    r = client.post(
        "/api/drift/run",
        json={},
        headers={"X-Cron-Token": "test-cron-secret"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["triggered_by"] == "cron"
    assert body["enqueued"] == 2
    assert {uid for _, uid in pool.store["enqueued"]} == {
        FAKE_USER.user_id,
        OTHER_USER.user_id,
    }


def test_run_drift_with_bad_cron_token_returns_401(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool
    _bind_queue(main_module, pool.store)

    r = client.post(
        "/api/drift/run",
        json={},
        headers={"X-Cron-Token": "wrong-secret"},
    )
    assert r.status_code == 401


def test_run_drift_with_jwt_enqueues_one_job_for_caller(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool
    _bind_queue(main_module, pool.store)

    r = client.post(
        "/api/drift/run",
        json={"user_ids": ["other_user"]},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["triggered_by"] == "user"
    assert body["enqueued"] == 1
    # user_ids in body is ignored; we enqueue for the caller only.
    assert pool.store["enqueued"] == [("drift.scan", FAKE_USER.user_id)]


def test_list_drift_events(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    _seed_event(pool, what_changed="branch_protection removed")
    _seed_event(pool, user_id=OTHER_USER.user_id, what_changed="cross-tenant row")

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/drift/events", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["control_id"] == "CC6.1"


def test_dismiss_drift_event(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    eid = _seed_event(pool)

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/drift/events/{eid}",
        json={"status": "dismissed", "reason": "False positive — already mitigated"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dismissed"


def test_dismiss_without_reason_returns_422(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    eid = _seed_event(pool)

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/drift/events/{eid}",
        json={"status": "dismissed"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 422


def test_resolve_event(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    eid = _seed_event(pool)

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/drift/events/{eid}",
        json={"status": "resolved"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"


def test_dismiss_missing_event_returns_404(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/drift/events/{uuid.uuid4()}",
        json={"status": "resolved"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 404


def test_get_monitored_returns_optouts(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    pool.store["monitored"][(FAKE_USER.user_id, "CC6.1")] = False
    pool.store["monitored"][(FAKE_USER.user_id, "CC6.2")] = False

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/drift/monitored", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200
    assert sorted(r.json()["optouts"]) == ["CC6.1", "CC6.2"]


def test_get_drift_status_returns_heartbeat(monkeypatch):
    """Sprint 9 post-deploy UX fix — GET /api/drift/status returns the
    baseline count + last_scan_at + event totals so the FE can confirm
    the watcher actually ran on a baseline-only first scan.
    """
    client = _make_client(monkeypatch)
    pool = _Pool()
    # Seed a baseline-only run: 3 snapshots, 0 events.
    now = datetime.now(UTC)
    pool.store["snapshots"] = {
        (FAKE_USER.user_id, "CC6.1"): {"last_seen_at": now},
        (FAKE_USER.user_id, "CC6.2"): {"last_seen_at": now},
        (FAKE_USER.user_id, "CC7.1"): {"last_seen_at": now},
    }

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/drift/status", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baselines"] == 3
    assert body["events_total"] == 0
    assert body["events_open"] == 0
    assert body["last_scan_at"] is not None


def test_get_drift_status_with_events(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()
    _seed_event(pool)  # 1 open event
    _seed_event(pool, status="resolved")

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/drift/status", headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events_total"] == 2
    assert body["events_open"] == 1


def test_patch_monitored_sets_optout(monkeypatch):
    client = _make_client(monkeypatch)
    pool = _Pool()

    import apps.api.main as main_module
    from apps.api.db import get_pool

    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        "/api/drift/monitored/CC6.1",
        json={"monitored": False},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200
    assert r.json() == {"control_id": "CC6.1", "monitored": False}
    assert pool.store["monitored"][(FAKE_USER.user_id, "CC6.1")] is False
