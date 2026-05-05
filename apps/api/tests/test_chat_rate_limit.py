"""Sprint 3 day-0 chunk 3.0a — slowapi rate limit on /chat.

OWASP LLM10 (Unbounded Consumption) mitigation. Sprint 2 left ``/chat``
unauthenticated by design — Clerk JWT verification lands in Sprint 3 chunk
3.5. Until then, the ``@limiter.limit(_chat_rate_limit)`` decorator caps
requests at ``CHAT_RATE_LIMIT`` (default ``10/minute``) per remote IP.

The test sets the limit to ``2/minute`` so the third request from the same
client returns HTTP 429 within a single test run.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def chat_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Reload the app with ``CHAT_RATE_LIMIT=2/minute`` and a stub graph.

    Setting the env var BEFORE importing ``apps.api.main`` is required —
    ``_chat_rate_limit()`` is called by slowapi at request time, but the
    ``Limiter`` instance is constructed at import time. ``monkeypatch.setenv``
    handles the lifecycle.
    """

    monkeypatch.setenv("CHAT_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DATABASE_URL", "postgres://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", "pk_test_fake")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake")

    # Re-import main so the module-level Limiter sees the env. Use importlib
    # to avoid cross-test cache pollution.
    import importlib

    import apps.api.main as main_module

    main_module = importlib.reload(main_module)
    # Reset the slowapi storage so each test starts at zero requests served.
    main_module.limiter.reset()

    # Replace the streaming chat handler with one that returns immediately so
    # the test does not depend on Gemini, Langfuse, Postgres, or Redis. The
    # rate-limit decorator runs BEFORE the route body, so swapping the body
    # does not weaken the test — the 429 is produced inside the decorator.
    async def _passthrough_stream(*, req, thread_id):  # type: ignore[no-untyped-def]
        yield 'data: {"type":"finish"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(main_module, "_chat_stream_generator", _passthrough_stream)

    with TestClient(main_module.app) as client:
        yield client


def test_chat_rate_limit_returns_429_after_quota_exhausted(
    chat_client: TestClient,
) -> None:
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "intent": "free_chat",
    }

    # First two requests within the minute window — under the 2/minute cap.
    r1 = chat_client.post("/chat", json=body)
    r2 = chat_client.post("/chat", json=body)
    assert r1.status_code == 200, (
        f"first request must succeed; got {r1.status_code}: {r1.text[:200]}"
    )
    assert r2.status_code == 200, (
        f"second request must succeed; got {r2.status_code}: {r2.text[:200]}"
    )

    # Third request exceeds the limit — slowapi returns 429.
    r3 = chat_client.post("/chat", json=body)
    assert r3.status_code == 429, (
        f"third request must be rate-limited; got {r3.status_code}: {r3.text[:200]}"
    )


def test_chat_rate_limit_default_is_ten_per_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity-check the default — operator running without CHAT_RATE_LIMIT
    set must still get a non-trivial cap, not unlimited."""

    monkeypatch.delenv("CHAT_RATE_LIMIT", raising=False)
    from apps.api.main import _chat_rate_limit

    assert _chat_rate_limit() == "10/minute"
