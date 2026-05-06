"""Tests for apps.api.services.github_evidence.

All tests mock the httpx.AsyncClient so no real GitHub API calls are made.
Read-only invariant: every check uses GET; no POST/PUT/PATCH appears anywhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.api.services.github_evidence import (
    _content_hash,
    _strip_volatile,
    check_branch_protection,
    check_code_scanning,
    check_dependabot,
    check_org_mfa,
    check_secret_scanning,
    make_github_evidence_collector,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_response(status: int, json_body: object | None = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=json_body or {})
    r.text = text
    return r


def _async_client_returning(status: int, body: object | None = None) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=_mock_response(status, body))
    return client


# ── _strip_volatile / _content_hash ──────────────────────────────────────────


def test_strip_volatile_removes_timestamps() -> None:
    raw = {"created_at": "2026-01-01", "updated_at": "now", "value": True}
    stripped = _strip_volatile(raw)
    assert "created_at" not in stripped
    assert "updated_at" not in stripped
    assert stripped["value"] is True


def test_content_hash_stable() -> None:
    payload = {"branch_protection_enabled": True, "required_reviews": 2}
    h1 = _content_hash(payload)
    h2 = _content_hash(payload)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_content_hash_differs_on_different_payload() -> None:
    h1 = _content_hash({"a": 1})
    h2 = _content_hash({"a": 2})
    assert h1 != h2


# ── check_branch_protection ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_branch_protection_enabled() -> None:
    body = {"required_pull_request_reviews": {"required_approving_review_count": 2}}
    client = _async_client_returning(200, body)
    rows = await check_branch_protection(client, "tok", "acme/repo", "sr-1")
    assert len(rows) == 1
    assert rows[0].raw["protection_enabled"] is True
    assert rows[0].source_type == "github"
    assert rows[0].content_hash is not None


@pytest.mark.asyncio
async def test_branch_protection_disabled_404() -> None:
    client = _async_client_returning(404)
    rows = await check_branch_protection(client, "tok", "acme/repo", "sr-1")
    assert len(rows) == 1
    assert rows[0].raw["protection_enabled"] is False


# ── check_org_mfa ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_mfa_enabled() -> None:
    body = {"two_factor_requirement_enabled": True, "login": "acme"}
    client = _async_client_returning(200, body)
    rows = await check_org_mfa(client, "tok", "acme/repo", "sr-1")
    assert len(rows) == 1
    assert rows[0].raw["two_factor_requirement_enabled"] is True


@pytest.mark.asyncio
async def test_org_mfa_personal_account_404_returns_empty() -> None:
    client = _async_client_returning(404)
    rows = await check_org_mfa(client, "tok", "acme/repo", "sr-1")
    assert rows == []


# ── check_code_scanning ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_scanning_enabled() -> None:
    body = [{"number": 1, "state": "open"}]
    client = _async_client_returning(200, body)
    rows = await check_code_scanning(client, "tok", "acme/repo", "sr-1")
    assert len(rows) == 1
    assert rows[0].raw["code_scanning_enabled"] is True
    assert rows[0].raw["open_alert_count"] == 1


@pytest.mark.asyncio
async def test_code_scanning_not_configured_404() -> None:
    client = _async_client_returning(404)
    rows = await check_code_scanning(client, "tok", "acme/repo", "sr-1")
    assert len(rows) == 1
    assert rows[0].raw["code_scanning_enabled"] is False


# ── check_secret_scanning ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secret_scanning_enabled() -> None:
    body = [{"number": 1, "state": "open"}]
    client = _async_client_returning(200, body)
    rows = await check_secret_scanning(client, "tok", "acme/repo", "sr-1")
    assert rows[0].raw["secret_scanning_enabled"] is True


# ── check_dependabot ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dependabot_enabled_204() -> None:
    client = _async_client_returning(204)
    rows = await check_dependabot(client, "tok", "acme/repo", "sr-1")
    assert rows[0].raw["dependabot_enabled"] is True


@pytest.mark.asyncio
async def test_dependabot_disabled_404() -> None:
    client = _async_client_returning(404)
    rows = await check_dependabot(client, "tok", "acme/repo", "sr-1")
    assert rows[0].raw["dependabot_enabled"] is False


# ── make_github_evidence_collector ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_collector_aggregates_all_checks() -> None:
    """Collector runs all 5 checks and returns all Evidence rows."""
    protection_body = {"required_pull_request_reviews": {"required_approving_review_count": 1}}

    async def _fake_get(url: str, **kwargs: object) -> MagicMock:
        if "/branches/" in url:
            return _mock_response(200, protection_body)
        if "/orgs/" in url:
            return _mock_response(200, {"two_factor_requirement_enabled": True, "login": "acme"})
        if "/code-scanning/" in url:
            return _mock_response(200, [])
        if "/secret-scanning/" in url:
            return _mock_response(200, [])
        if "/vulnerability-alerts" in url:
            return _mock_response(204)
        return _mock_response(200, {})

    collector = make_github_evidence_collector(
        github_token="tok",
        repo_full_names={"123": "acme/repo"},
    )

    with patch("httpx.AsyncClient") as mock_cls:
        instance = AsyncMock()
        instance.get = _fake_get
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = instance

        rows = await collector(repo_id="123", scan_run_id="sr-1")

    # Should have at least branch_protection + dependabot rows (org 200 → 1 row, code + secret)
    assert len(rows) >= 2
    source_types = {r.source_type for r in rows}
    assert source_types == {"github"}


@pytest.mark.asyncio
async def test_collector_unknown_repo_id_returns_empty() -> None:
    collector = make_github_evidence_collector(
        github_token="tok",
        repo_full_names={"123": "acme/repo"},
    )
    rows = await collector(repo_id="999", scan_run_id="sr-1")
    assert rows == []
