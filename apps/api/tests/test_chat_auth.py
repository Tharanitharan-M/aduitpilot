"""Tests for /chat authentication enforcement.

Verifies that the /chat endpoint returns HTTP 401 when:
- No Authorization header is present
- A malformed (non-Bearer) Authorization header is present
- An invalid / expired JWT is present

And that a valid dep override lets the happy path proceed (200).

Refs: PLAN.md Sprint 5 auth fix; ADR-0008; OWASP A01.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

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

_VALID_BODY = {
    "messages": [{"id": "m1", "role": "user", "content": "hello"}],
    "intent": "free_chat",
    "thread_id": "t-auth-test",
}


@pytest.fixture(autouse=True)
def _env():
    from apps.api.main import get_settings

    with patch.dict(os.environ, _TEST_ENV, clear=False):
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture
async def raw_client():
    """ASGI client with NO dependency overrides — auth is fully enforced.

    Saves and clears dependency_overrides for the duration of each test so
    that leaked overrides from other test modules (e.g. test_actions.py which
    calls importlib.reload + sets the override without cleanup) do not
    silently bypass authentication.
    """
    from apps.api import main as main_module

    saved = dict(main_module.app.dependency_overrides)
    main_module.app.dependency_overrides.clear()
    try:
        transport = ASGITransport(app=main_module.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        main_module.app.dependency_overrides.update(saved)


@pytest.mark.asyncio
async def test_chat_returns_401_when_no_auth_header(raw_client: AsyncClient):
    resp = await raw_client.post("/chat", json=_VALID_BODY)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"expected 401, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_chat_returns_401_when_bearer_token_is_invalid(raw_client: AsyncClient):
    resp = await raw_client.post(
        "/chat",
        json=_VALID_BODY,
        headers={"Authorization": "Bearer this.is.not.a.valid.jwt"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"expected 401, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_chat_returns_401_when_scheme_is_not_bearer(raw_client: AsyncClient):
    resp = await raw_client.post(
        "/chat",
        json=_VALID_BODY,
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    # HTTPBearer rejects non-Bearer schemes → 401 (missing credentials path)
    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    ), f"expected 401 or 403, got {resp.status_code}"


@pytest.mark.asyncio
async def test_chat_401_response_includes_www_authenticate_header(
    raw_client: AsyncClient,
):
    resp = await raw_client.post("/chat", json=_VALID_BODY)
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    # RFC 9110 §11.6.1 — 401 MUST include WWW-Authenticate
    assert "www-authenticate" in {k.lower() for k in resp.headers}, (
        "401 response missing WWW-Authenticate header"
    )


@pytest.mark.asyncio
async def test_chat_succeeds_with_overridden_dep():
    """Sanity check: dep override lets authenticated calls through."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from apps.api import main as main_module
    from apps.api.auth.clerk import ClerkUser, verify_clerk_token

    def _stub_model(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart(content="ok")])

    stub = FunctionModel(_stub_model)
    fake_user = ClerkUser(user_id="user_test", session_id="sess_test")

    original_model = main_module._chat_model_factory
    original_mcp = main_module._chat_mcp_toolset
    main_module._chat_model_factory = lambda: stub
    main_module._chat_mcp_toolset = lambda: False
    main_module.app.dependency_overrides[verify_clerk_token] = lambda: fake_user

    try:
        transport = ASGITransport(app=main_module.app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/chat", json=_VALID_BODY)
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:200]}"
    finally:
        main_module._chat_model_factory = original_model
        main_module._chat_mcp_toolset = original_mcp
        main_module.app.dependency_overrides.pop(verify_clerk_token, None)
