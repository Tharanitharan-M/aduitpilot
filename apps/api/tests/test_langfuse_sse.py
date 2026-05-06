"""
Tests for chunk 2.8 — Langfuse trace propagation into the SSE finish event.

Acceptance (PLAN.md chunk 2.8):
  Invoke /chat; the finish SSE chunk's `messageMetadata` carries a
  `trace_id` and `trace_url` so the frontend can deeplink operators to
  the Langfuse trace. Manual: the same trace appears in Langfuse Cloud
  within 30s of invocation.

Strategy: patch `apps.api.observability.langfuse._current_client` to
return a stub Langfuse client that surfaces a known trace id. This keeps
the test hermetic (no Langfuse network calls) while exercising the full
propagation path from observation → finish_metadata_cb → SSE finish chunk.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

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

_FAKE_TRACE_ID = "00000000000000000000000000abc123"
_FAKE_TRACE_URL = (
    f"https://us.cloud.langfuse.com/trace/{_FAKE_TRACE_ID}"
)


@pytest.fixture(autouse=True)
def _env():
    from apps.api.main import get_settings

    with patch.dict(os.environ, _TEST_ENV, clear=False):
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
def lookup_then_reply_model():
    call_count = {"n": 0}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="lookup_control",
                        args={"control_id": "AC-1"},
                        tool_call_id="call_1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="AC-1 is Policy and Procedures.")])

    return FunctionModel(fn)


@pytest.fixture
def fake_langfuse_client():
    """A stub satisfying the subset of the Langfuse v4 API that traced_chat uses."""

    client = MagicMock()
    client.get_current_trace_id.return_value = _FAKE_TRACE_ID
    client.get_trace_url.return_value = _FAKE_TRACE_URL

    @contextmanager
    def _span_cm(**kwargs):
        span = MagicMock()
        yield span

    client.start_as_current_observation.side_effect = _span_cm
    return client


@pytest.fixture
async def client(lookup_then_reply_model, fake_langfuse_client):
    from apps.api import main as main_module
    from apps.api.auth.clerk import ClerkUser, verify_clerk_token
    from apps.api.observability import langfuse as lf_module

    _fake_user = ClerkUser(user_id="user_test", session_id="sess_test")

    original_model = main_module._chat_model_factory
    original_mcp = main_module._chat_mcp_toolset
    main_module._chat_model_factory = lambda: lookup_then_reply_model
    main_module._chat_mcp_toolset = lambda: False
    main_module.app.dependency_overrides[verify_clerk_token] = lambda: _fake_user

    with patch.object(lf_module, "_INITIALISED", fake_langfuse_client):
        transport = ASGITransport(
            app=main_module.app, raise_app_exceptions=False
        )
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    main_module._chat_model_factory = original_model
    main_module._chat_mcp_toolset = original_mcp
    main_module.app.dependency_overrides.pop(verify_clerk_token, None)


async def _consume(client: AsyncClient, body: dict) -> list[dict | str]:
    data_lines: list[str] = []
    async with client.stream("POST", "/chat", json=body) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
    out: list[dict | str] = []
    for raw in data_lines:
        if raw.strip() == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(raw))
    return out


@pytest.mark.asyncio
async def test_finish_chunk_carries_langfuse_trace_id_and_url(client: AsyncClient):
    chunks = await _consume(
        client,
        body={
            "messages": [{"role": "user", "content": "look up AC-1"}],
            "thread_id": "thread-trace",
        },
    )
    finish = next(
        c for c in chunks if isinstance(c, dict) and c.get("type") == "finish"
    )

    assert "messageMetadata" in finish, (
        f"finish chunk must surface metadata carrying the trace id; got {finish}"
    )
    md = finish["messageMetadata"]
    assert md.get("trace_id") == _FAKE_TRACE_ID, (
        f"expected Langfuse trace_id in finish.messageMetadata; got {md}"
    )
    assert md.get("trace_url") == _FAKE_TRACE_URL
    assert md.get("thread_id") == "thread-trace"


@pytest.mark.asyncio
async def test_finish_metadata_ommits_trace_when_langfuse_disabled(
    lookup_then_reply_model,
):
    """With fake Langfuse keys (ADR-0011 offline fallback), no trace_id fires."""

    from apps.api import main as main_module
    from apps.api.auth.clerk import ClerkUser, verify_clerk_token
    from apps.api.observability import langfuse as lf_module

    _fake_user = ClerkUser(user_id="user_test", session_id="sess_test")

    original_model = main_module._chat_model_factory
    original_mcp = main_module._chat_mcp_toolset
    main_module._chat_model_factory = lambda: lookup_then_reply_model
    main_module._chat_mcp_toolset = lambda: False
    main_module.app.dependency_overrides[verify_clerk_token] = lambda: _fake_user
    # Explicitly disable Langfuse
    with patch.object(lf_module, "_INITIALISED", None):
        transport = ASGITransport(
            app=main_module.app, raise_app_exceptions=False
        )
        try:
            async with AsyncClient(transport=transport, base_url="http://t") as ac:
                chunks = await _consume(
                    ac,
                    body={
                        "messages": [
                            {"role": "user", "content": "look up AC-1"}
                        ],
                        "thread_id": "thread-offline",
                    },
                )
        finally:
            main_module._chat_model_factory = original_model
            main_module._chat_mcp_toolset = original_mcp
            main_module.app.dependency_overrides.pop(verify_clerk_token, None)

    finish = next(
        c for c in chunks if isinstance(c, dict) and c.get("type") == "finish"
    )
    md = finish.get("messageMetadata", {})
    assert "trace_id" not in md, (
        f"Langfuse-disabled runs must not surface a fake trace_id; got {md}"
    )
    assert md.get("thread_id") == "thread-offline"
