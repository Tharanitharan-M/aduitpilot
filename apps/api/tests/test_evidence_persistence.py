"""Tests for apps.api.services.evidence_persistence.

Pool and httpx are mocked — no real DB or Gemini API calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.api.services.evidence_persistence import (
    _evidence_to_embed_text,
    _generate_embedding,
    persist_evidence,
)
from apps.api.state import Evidence

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _ev(idx: int = 0) -> Evidence:
    return Evidence(
        id=f"ev-{idx}",
        source_type="github",
        source_uri="github://acme/repo",
        raw={"branch_protection_enabled": True, "required_reviews": 2},
        content_hash="a" * 64,
        collected_at=datetime(2026, 5, 1, tzinfo=UTC),
        scan_run_id="sr-001",
    )


def _mock_pool(rowcount: int = 1) -> MagicMock:
    cursor = MagicMock()
    cursor.rowcount = rowcount
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=cursor)
    pool = MagicMock()
    pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


# ── _evidence_to_embed_text ───────────────────────────────────────────────────


def test_embed_text_contains_source_type() -> None:
    ev = _ev()
    text = _evidence_to_embed_text(ev)
    assert "source_type:github" in text


def test_embed_text_contains_uri() -> None:
    ev = _ev()
    text = _evidence_to_embed_text(ev)
    assert "uri:github://acme/repo" in text


def test_embed_text_contains_flat_raw_keys() -> None:
    ev = _ev()
    text = _evidence_to_embed_text(ev)
    assert "branch_protection_enabled:True" in text


def test_embed_text_no_uri() -> None:
    ev = _ev()
    ev.source_uri = None
    text = _evidence_to_embed_text(ev)
    assert "uri:" not in text


# ── _generate_embedding ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_embedding_success() -> None:
    fake_values = [0.1] * 768
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = MagicMock(return_value={"embedding": {"values": fake_values}})

    with patch("httpx.AsyncClient") as mock_cls:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=fake_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = instance

        result = await _generate_embedding("branch protection enabled", "fake-key")

    assert result is not None
    assert len(result) == 768


@pytest.mark.asyncio
async def test_generate_embedding_non_200_returns_none() -> None:
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    fake_resp.text = "rate limited"

    with patch("httpx.AsyncClient") as mock_cls:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=fake_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = instance

        result = await _generate_embedding("text", "fake-key")

    assert result is None


@pytest.mark.asyncio
async def test_generate_embedding_empty_text_returns_none() -> None:
    result = await _generate_embedding("  ", "fake-key")
    assert result is None


# ── persist_evidence ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_evidence_empty_list_returns_zero() -> None:
    pool = _mock_pool()
    count = await persist_evidence([], "user_1", pool)
    assert count == 0


@pytest.mark.asyncio
async def test_persist_evidence_inserts_rows_no_embedding() -> None:
    pool = _mock_pool(rowcount=1)
    ev = _ev()
    count = await persist_evidence([ev], "user_1", pool, gemini_api_key=None)
    assert count == 1
    assert ev.user_id == "user_1"


@pytest.mark.asyncio
async def test_persist_evidence_conflict_zero_rowcount() -> None:
    """ON CONFLICT DO NOTHING → rowcount 0 → inserted stays 0."""
    pool = _mock_pool(rowcount=0)
    count = await persist_evidence([_ev()], "user_1", pool, gemini_api_key=None)
    assert count == 0


@pytest.mark.asyncio
async def test_persist_evidence_with_embedding() -> None:
    fake_values = [0.5] * 768
    pool = _mock_pool(rowcount=1)

    with patch(
        "apps.api.services.evidence_persistence._generate_embedding",
        new=AsyncMock(return_value=fake_values),
    ):
        count = await persist_evidence(
            [_ev()], "user_1", pool, gemini_api_key="fake-key"
        )
    assert count == 1
