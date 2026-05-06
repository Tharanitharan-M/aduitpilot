"""
Sprint 4 chunk 4.9 — client-disconnect cancellation tests.

The /chat handler polls ``request.is_disconnected()`` every 5 s while
streaming. When the client drops, the disconnect watcher fires
asyncio.Event.set(), the producer loop checks the event before its
next yield, and the response stream stops without further LLM calls.

These tests stub the ``Request`` shape with a ``_FakeRequest`` whose
``is_disconnected`` flips True after a controllable count, simulating
a mid-stream client drop without needing a real TCP socket.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

_TEST_ENV = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "postgres://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CLERK_SECRET_KEY": "sk_test_fake",
    "CLERK_PUBLISHABLE_KEY": "pk_test_fake",
    "GEMINI_API_KEY": "fake-key",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-fake",
    "LANGFUSE_SECRET_KEY": "sk-lf-fake",
}


@pytest.fixture(autouse=True)
def _env():
    from apps.api.main import get_settings

    with patch.dict(os.environ, _TEST_ENV, clear=False):
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


class _FakeRequest:
    """Stand-in for FastAPI's :class:`Request`.

    ``is_disconnected`` returns False for the first
    ``flip_after_calls`` invocations, then True. The chat handler
    polls this in a sidecar task; flipping it mid-stream simulates the
    user closing the browser tab.
    """

    def __init__(self, *, flip_after_calls: int = 1) -> None:
        self._calls = 0
        self._flip_after = flip_after_calls

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._flip_after


async def _slow_producer() -> AsyncIterator[str]:
    """Stand-in for the SSE producer.

    Yields one chunk and then ``await``s a long sleep — long enough
    that the disconnect watcher (with poll interval 0.05 s) has time
    to fire and cancel the consumer before any further chunk is
    emitted.
    """

    import asyncio

    yield "data: chunk-0\n\n"
    # Simulate the LLM thinking. Long enough for the watcher to fire,
    # short enough to keep the test fast.
    await asyncio.sleep(0.5)
    yield "data: chunk-1\n\n"
    yield "data: chunk-2\n\n"


@pytest.mark.asyncio
async def test_disconnect_watcher_short_circuits_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the client drops, the SSE generator stops yielding."""

    import apps.api.main as main_module

    # Bypass the full graph build — replace the SSE producer with a
    # controllable async generator that yields slowly.
    monkeypatch.setattr(
        main_module,
        "ui_message_stream_from_graph_updates",
        lambda *a, **kw: _slow_producer(),
    )
    monkeypatch.setattr(main_module, "_chat_mcp_toolset", lambda: False)
    monkeypatch.setattr(
        main_module,
        "_chat_model_factory",
        lambda: "test",
    )
    # Bypass the live Langfuse handle (offline mode is fine here).

    req = main_module.ChatRequest(
        messages=[main_module.ChatMessage(role="user", content="hi")],
        intent="free_chat",
    )
    fake_request = _FakeRequest(flip_after_calls=1)

    chunks: list[str] = []
    async for chunk in main_module._chat_stream_generator(
        req=req,
        thread_id="t-disco",
        request=fake_request,  # type: ignore[arg-type]
        disconnect_poll_interval_s=0.05,
    ):
        chunks.append(chunk)

    # The producer's first chunk lands; subsequent chunks must NOT
    # appear because the watcher cancelled the stream.
    assert chunks[0] == "data: chunk-0\n\n"
    assert "data: chunk-2\n\n" not in chunks, (
        f"client disconnect did not stop the stream; got {chunks!r}"
    )


@pytest.mark.asyncio
async def test_no_disconnect_lets_stream_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the client stays connected, every chunk is delivered."""

    import apps.api.main as main_module

    monkeypatch.setattr(
        main_module,
        "ui_message_stream_from_graph_updates",
        lambda *a, **kw: _slow_producer(),
    )
    monkeypatch.setattr(main_module, "_chat_mcp_toolset", lambda: False)
    monkeypatch.setattr(
        main_module,
        "_chat_model_factory",
        lambda: "test",
    )

    class _AlwaysConnected:
        async def is_disconnected(self) -> bool:
            return False

    req = main_module.ChatRequest(
        messages=[main_module.ChatMessage(role="user", content="hi")],
        intent="free_chat",
    )
    chunks: list[str] = []
    async for chunk in main_module._chat_stream_generator(
        req=req,
        thread_id="t-connected",
        request=_AlwaysConnected(),  # type: ignore[arg-type]
        disconnect_poll_interval_s=0.05,
    ):
        chunks.append(chunk)

    assert chunks == [
        "data: chunk-0\n\n",
        "data: chunk-1\n\n",
        "data: chunk-2\n\n",
    ]


@pytest.mark.asyncio
async def test_no_request_falls_back_to_unconditional_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no request handle is provided, the generator runs as before."""

    import apps.api.main as main_module

    monkeypatch.setattr(
        main_module,
        "ui_message_stream_from_graph_updates",
        lambda *a, **kw: _slow_producer(),
    )
    monkeypatch.setattr(main_module, "_chat_mcp_toolset", lambda: False)
    monkeypatch.setattr(
        main_module,
        "_chat_model_factory",
        lambda: "test",
    )

    req = main_module.ChatRequest(
        messages=[main_module.ChatMessage(role="user", content="hi")],
        intent="free_chat",
    )
    chunks: list[str] = []
    async for chunk in main_module._chat_stream_generator(
        req=req,
        thread_id="t-no-request",
        request=None,
        disconnect_poll_interval_s=0.05,
    ):
        chunks.append(chunk)

    assert chunks == [
        "data: chunk-0\n\n",
        "data: chunk-1\n\n",
        "data: chunk-2\n\n",
    ]
