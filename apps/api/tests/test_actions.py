"""
Sprint 4 chunks 4.7 + 4.8 — Pending Actions queue tests.

Covers:
  - GET /api/actions returns the user's actions (status filter optional).
  - PATCH /api/actions/{id} applies state machine transitions.
  - Invalid transitions return 409 with a typed body.
  - rejected/revoked transitions require a reason (422 otherwise).
  - Cross-user reads / writes return 404 (RLS-style isolation).

Uses an in-memory ``_FakePool`` that emulates the SQL the handler
issues, mirroring the pattern in ``test_connectors.py``.
"""

from __future__ import annotations

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

    import importlib

    import apps.api.main as main_module
    main_module = importlib.reload(main_module)

    from apps.api.auth.clerk import verify_clerk_token
    main_module.app.dependency_overrides[verify_clerk_token] = lambda: user

    return TestClient(main_module.app)


# ── In-memory fake pool that emulates the actions handler's SQL ─────────────


class _FakeCursor:
    def __init__(self, store: list[dict]) -> None:
        self._store = store
        self._rows: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())  # collapse whitespace

        if q.startswith("SELECT id::text, user_id, scan_run_id, kind"):
            # Either single-row by id, or list-by-user.
            if "id::text = %s" in q:
                user_id, action_id = params
                hit = [
                    r for r in self._store
                    if r["user_id"] == user_id and r["id"] == action_id
                ]
                self._rows = [_row_tuple(r) for r in hit]
            else:
                if "AND status = %s" in q:
                    user_id, status_filter, _limit = params
                    hits = [
                        r for r in self._store
                        if r["user_id"] == user_id and r["status"] == status_filter
                    ]
                else:
                    user_id, _limit = params
                    hits = [r for r in self._store if r["user_id"] == user_id]
                hits.sort(key=lambda r: r["created_at"], reverse=True)
                self._rows = [_row_tuple(r) for r in hits]
        elif q.startswith("SELECT status FROM actions"):
            user_id, action_id = params
            hit = [
                r for r in self._store
                if r["user_id"] == user_id and r["id"] == action_id
            ]
            self._rows = [(hit[0]["status"],)] if hit else []
        elif q.startswith("UPDATE actions SET status = %s, rejected_reason"):
            new_status, reason, user_id, action_id = params
            for r in self._store:
                if r["user_id"] == user_id and r["id"] == action_id:
                    r["status"] = new_status
                    r["rejected_reason"] = reason
                    r["updated_at"] = datetime.now(UTC)
        elif q.startswith("UPDATE actions SET status = %s, revoked_reason"):
            new_status, reason, user_id, action_id = params
            for r in self._store:
                if r["user_id"] == user_id and r["id"] == action_id:
                    r["status"] = new_status
                    r["revoked_reason"] = reason
                    r["revoked_at"] = datetime.now(UTC)
                    r["updated_at"] = datetime.now(UTC)
        elif q.startswith("UPDATE actions SET status = %s, updated_at"):
            new_status, user_id, action_id = params
            for r in self._store:
                if r["user_id"] == user_id and r["id"] == action_id:
                    r["status"] = new_status
                    r["updated_at"] = datetime.now(UTC)
        else:
            raise AssertionError(f"unexpected SQL: {q!r}")

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


def _row_tuple(r: dict) -> tuple:
    return (
        r["id"],
        r["user_id"],
        r.get("scan_run_id"),
        r["kind"],
        r["title"],
        r.get("description", ""),
        r["status"],
        r.get("tsc_id"),
        r.get("source_link"),
        r.get("rejected_reason"),
        r.get("revoked_reason"),
        r.get("revoked_at"),
        r["created_at"],
        r["updated_at"],
    )


class _FakeConn:
    def __init__(self, store: list[dict]) -> None:
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self._store)

    def transaction(self):
        return self

    async def commit(self):
        return None


class _FakePool:
    def __init__(self) -> None:
        self.store: list[dict] = []

    def connection(self):
        return _FakeConn(self.store)

    def seed(self, **fields: Any) -> str:
        action_id = fields.pop("id", str(uuid.uuid4()))
        now = datetime.now(UTC)
        record = {
            "id": action_id,
            "user_id": fields.pop("user_id", FAKE_USER.user_id),
            "scan_run_id": fields.pop("scan_run_id", None),
            "kind": fields.pop("kind", "enable_branch_protection"),
            "title": fields.pop(
                "title", "Enable required PR review on the main branch"
            ),
            "description": fields.pop(
                "description",
                "main branch has no required reviewer policy.",
            ),
            "status": fields.pop("status", "pending_review"),
            "tsc_id": fields.pop("tsc_id", "CC6.1"),
            "source_link": fields.pop("source_link", None),
            "rejected_reason": fields.pop("rejected_reason", None),
            "revoked_reason": fields.pop("revoked_reason", None),
            "revoked_at": fields.pop("revoked_at", None),
            "created_at": fields.pop("created_at", now),
            "updated_at": fields.pop("updated_at", now),
        }
        if fields:
            raise AssertionError(f"unexpected seed fields: {fields}")
        self.store.append(record)
        return action_id


# ── GET /api/actions ────────────────────────────────────────────────────────


def test_list_actions_returns_users_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    pool.seed(title="Action 1")
    pool.seed(title="Action 2", status="approved")
    # Cross-tenant row that must NOT appear.
    pool.seed(user_id=OTHER_USER.user_id, title="Other user's action")

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get("/api/actions", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    titles = sorted(a["title"] for a in body["actions"])
    assert titles == ["Action 1", "Action 2"]


def test_list_actions_filters_by_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    pool.seed(title="Pending")
    pool.seed(title="Done", status="completed")

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.get(
        "/api/actions?status=completed",
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["actions"][0]["title"] == "Done"


# ── PATCH /api/actions/{id} — happy paths ───────────────────────────────────


@pytest.mark.parametrize(
    "from_status,to_status,reason,expected_field",
    [
        ("pending_review", "approved", None, None),
        ("pending_review", "completed", None, None),
        ("approved", "completed", None, None),
        ("pending_review", "rejected", "duplicate of #321", "rejected_reason"),
    ],
)
def test_patch_action_valid_transitions(
    monkeypatch: pytest.MonkeyPatch,
    from_status: str,
    to_status: str,
    reason: str | None,
    expected_field: str | None,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    action_id = pool.seed(status=from_status)

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    body: dict[str, Any] = {"status": to_status}
    if reason is not None:
        body["reason"] = reason

    r = client.patch(
        f"/api/actions/{action_id}",
        json=body,
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["status"] == to_status
    if expected_field == "rejected_reason":
        assert payload["rejected_reason"] == reason


# ── PATCH /api/actions/{id} — invalid transitions return 409 ───────────────


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("rejected", "approved"),       # terminal
        ("revoked", "approved"),        # terminal
        ("approved", "rejected"),       # not in allowed set
        ("pending_review", "revoked"),  # must go through completed first
        ("completed", "approved"),      # backwards
    ],
)
def test_patch_action_invalid_transitions_return_409(
    monkeypatch: pytest.MonkeyPatch,
    from_status: str,
    to_status: str,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    action_id = pool.seed(status=from_status)

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/actions/{action_id}",
        json={"status": to_status, "reason": "trying anyway"},
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_transition"
    assert detail["from_status"] == from_status
    assert detail["to_status"] == to_status


def test_patch_action_rejected_without_reason_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    action_id = pool.seed(status="pending_review")

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/actions/{action_id}",
        json={"status": "rejected"},  # missing reason
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "reason_required"
    assert detail["to_status"] == "rejected"


def test_patch_action_404_when_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()
    other_action_id = pool.seed(user_id=OTHER_USER.user_id)

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        f"/api/actions/{other_action_id}",
        json={"status": "approved"},
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 404


def test_patch_action_unknown_id_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    pool = _FakePool()

    import apps.api.main as main_module
    from apps.api.db import get_pool
    main_module.app.dependency_overrides[get_pool] = lambda: pool

    r = client.patch(
        "/api/actions/00000000-0000-0000-0000-000000000000",
        json={"status": "approved"},
        headers={"Authorization": "Bearer fake"},
    )

    assert r.status_code == 404


# ── State machine pure predicate ────────────────────────────────────────────


def test_state_machine_table_matches_us007_us032() -> None:
    """The contract written into US-007 / US-032 must match the table."""

    from apps.api.routes.actions import _ALLOWED_TRANSITIONS, _is_transition_allowed

    # US-007 — pending_review can go to approved / rejected / completed.
    assert _is_transition_allowed("pending_review", "approved")
    assert _is_transition_allowed("pending_review", "rejected")
    assert _is_transition_allowed("pending_review", "completed")
    # approved → completed only.
    assert _is_transition_allowed("approved", "completed")
    assert not _is_transition_allowed("approved", "rejected")
    # US-032 — completed can revoke; rejected and revoked are terminal.
    assert _is_transition_allowed("completed", "revoked")
    assert _ALLOWED_TRANSITIONS["rejected"] == frozenset()
    assert _ALLOWED_TRANSITIONS["revoked"] == frozenset()
