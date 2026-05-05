"""
Sprint 3 chunks 3.7, 3.8 — GET /api/me and DELETE /api/connectors/:id.

Uses respx to mock the Clerk Backend API and monkeypatches
``verify_clerk_token`` so tests never hit a real JWKS endpoint.

Refs: PLAN.md chunks 3.7, 3.8; ADR-0008.
"""

from __future__ import annotations

import pytest
import respx
import httpx
from fastapi.testclient import TestClient

from apps.api.auth.clerk import ClerkUser


# ── Fixtures ──────────────────────────────────────────────────────────────────

FAKE_USER = ClerkUser(user_id="user_abc", session_id="sess_123")

GITHUB_ACCOUNT = {
    "id": "ext_github_001",
    "provider": "oauth_github",
    "verification": {"status": "verified"},
    # Clerk Backend API returns Unix milliseconds (int), not ISO 8601.
    # Verified live 2026-05-05 — `eac_3DHx1c…` returned 1777952584755.
    "updated_at": 1777952584755,
}


def _make_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient with auth dep overridden to return FAKE_USER."""
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
    main_module.app.dependency_overrides[verify_clerk_token] = lambda: FAKE_USER

    return TestClient(main_module.app)


# ── GET /api/me ───────────────────────────────────────────────────────────────

@respx.mock
def test_get_me_returns_connected_github_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(return_value=httpx.Response(200, json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]}))

    client = _make_client(monkeypatch)
    r = client.get("/api/me", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == FAKE_USER.user_id
    assert len(body["connectors"]) == 1
    conn = body["connectors"][0]
    assert conn["provider"] == "github"
    assert conn["status"] == "connected"
    assert conn["id"] == "ext_github_001"


@respx.mock
def test_get_me_returns_error_status_when_github_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    unverified = {**GITHUB_ACCOUNT, "verification": {"status": "failed"}}
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(return_value=httpx.Response(200, json={"id": FAKE_USER.user_id, "external_accounts": [unverified]}))

    client = _make_client(monkeypatch)
    r = client.get("/api/me", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 200
    conn = r.json()["connectors"][0]
    assert conn["status"] == "error"
    assert conn["error_message"] == "Re-authentication required"


@respx.mock
def test_get_me_returns_empty_connectors_when_no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(return_value=httpx.Response(200, json={"id": FAKE_USER.user_id, "external_accounts": []}))

    client = _make_client(monkeypatch)
    r = client.get("/api/me", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 200
    assert r.json()["connectors"] == []


# ── DELETE /api/connectors/:id ────────────────────────────────────────────────

@respx.mock
def test_delete_connector_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    ext_id = "ext_github_001"
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(return_value=httpx.Response(200, json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]}))
    respx.delete(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}/external_accounts/{ext_id}"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    client = _make_client(monkeypatch)
    r = client.delete(f"/api/connectors/{ext_id}", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 204


@respx.mock
def test_delete_connector_returns_404_when_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(return_value=httpx.Response(200, json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]}))

    client = _make_client(monkeypatch)
    r = client.delete("/api/connectors/ext_other_999", headers={"Authorization": "Bearer fake"})

    assert r.status_code == 404


# ── PATCH /api/connectors/:id/scoped-repos (Sprint 3.5.3) ────────────────────


class _FakeCursor:
    """Minimal psycopg-style async cursor for the PATCH route's pool path."""

    def __init__(self, store: list[dict]) -> None:
        self._store = store
        self._last_query: str = ""
        self._last_params: tuple = ()
        self._rows: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query: str, params: tuple = ()) -> None:
        self._last_query = query
        self._last_params = params or ()
        q = " ".join(query.split())
        if q.startswith("SELECT COUNT(*) FROM connector_scoped_repos"):
            user_id, connector_id = params
            self._rows = [
                (sum(1 for r in self._store if r["user_id"] == user_id and r["connector_id"] == connector_id),)
            ]
        elif q.startswith("SELECT provider_repo_id"):
            user_id, connector_id = params
            self._rows = [
                (r["provider_repo_id"], r["full_name"], r["private"])
                for r in self._store
                if r["user_id"] == user_id and r["connector_id"] == connector_id
            ]
            self._rows.sort(key=lambda r: r[1])
        elif "DELETE FROM connector_scoped_repos" in q and "NOT (provider_repo_id = ANY(" in q:
            user_id, connector_id, keep_ids = params
            keep_set = set(keep_ids)
            self._store[:] = [
                r for r in self._store
                if not (
                    r["user_id"] == user_id
                    and r["connector_id"] == connector_id
                    and r["provider_repo_id"] not in keep_set
                )
            ]
        elif "DELETE FROM connector_scoped_repos" in q:
            user_id, connector_id = params
            self._store[:] = [
                r for r in self._store
                if not (r["user_id"] == user_id and r["connector_id"] == connector_id)
            ]

    async def executemany(self, query: str, paramset: list[tuple]) -> None:
        for params in paramset:
            connector_id, user_id, provider_repo_id, full_name, private = params
            seen = any(
                r["connector_id"] == connector_id and r["provider_repo_id"] == provider_repo_id
                for r in self._store
            )
            if seen:
                continue  # ON CONFLICT DO NOTHING
            self._store.append(
                {
                    "connector_id": connector_id,
                    "user_id": user_id,
                    "provider_repo_id": provider_repo_id,
                    "full_name": full_name,
                    "private": private,
                }
            )

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


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


@respx.mock
def test_patch_scoped_repos_persists_selection_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ext_id = "ext_github_001"
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]},
        )
    )

    client = _make_client(monkeypatch)
    fake_pool = _FakePool()

    from apps.api.db import get_pool
    import apps.api.main as main_module
    main_module.app.dependency_overrides[get_pool] = lambda: fake_pool

    body = {
        "repos": [
            {"provider_repo_id": "111", "full_name": "acme/orders-api", "private": True},
            {"provider_repo_id": "222", "full_name": "acme/auth-service", "private": True},
        ]
    }
    r = client.patch(
        f"/api/connectors/{ext_id}/scoped-repos",
        json=body,
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert data["connector_id"] == ext_id
    full_names = sorted(repo["full_name"] for repo in data["repos"])
    assert full_names == ["acme/auth-service", "acme/orders-api"]

    # Idempotency: re-PATCH the same selection — same final state, no duplicates.
    r2 = client.patch(
        f"/api/connectors/{ext_id}/scoped-repos",
        json=body,
        headers={"Authorization": "Bearer fake"},
    )
    assert r2.status_code == 200
    assert r2.json()["count"] == 2

    # Replace flow: PATCH a smaller selection — old rows are removed.
    r3 = client.patch(
        f"/api/connectors/{ext_id}/scoped-repos",
        json={
            "repos": [
                {"provider_repo_id": "111", "full_name": "acme/orders-api", "private": True},
            ]
        },
        headers={"Authorization": "Bearer fake"},
    )
    assert r3.status_code == 200
    assert r3.json()["count"] == 1
    assert r3.json()["repos"][0]["full_name"] == "acme/orders-api"


@respx.mock
def test_patch_scoped_repos_returns_404_when_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]},
        )
    )

    client = _make_client(monkeypatch)
    fake_pool = _FakePool()
    from apps.api.db import get_pool
    import apps.api.main as main_module
    main_module.app.dependency_overrides[get_pool] = lambda: fake_pool

    r = client.patch(
        "/api/connectors/ext_other_999/scoped-repos",
        json={"repos": []},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 404


@respx.mock
def test_patch_scoped_repos_rejects_extra_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ScopedReposPatch is extra='forbid' — a misspelled or extra field
    should be rejected with 422 before any DB call is made."""
    respx.get(
        f"https://api.clerk.com/v1/users/{FAKE_USER.user_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": FAKE_USER.user_id, "external_accounts": [GITHUB_ACCOUNT]},
        )
    )

    client = _make_client(monkeypatch)
    fake_pool = _FakePool()
    from apps.api.db import get_pool
    import apps.api.main as main_module
    main_module.app.dependency_overrides[get_pool] = lambda: fake_pool

    r = client.patch(
        "/api/connectors/ext_github_001/scoped-repos",
        json={"repos": [], "rouge_field": "x"},
        headers={"Authorization": "Bearer fake"},
    )
    assert r.status_code == 422
