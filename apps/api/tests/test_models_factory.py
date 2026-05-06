"""Tests for the Sprint 4 chunk 4.3a model factory.

Acceptance (PLAN.md chunk 4.3a):
- ``parse_model_string`` accepts well-formed identifiers and rejects bad ones.
- ``build_model("google-gla:...", settings)`` returns a :class:`GoogleModel`.
- ``build_model("anthropic:...", settings)`` returns an :class:`AnthropicModel`.
- ``build_model("openai:...", settings)`` returns an :class:`OpenAIChatModel`.
- After ``build_model("google-gla:...")`` returns,
  ``os.environ.get("GOOGLE_API_KEY")`` is unset (subprocess-leakage closed).
- Missing API key for an optional provider raises :class:`MissingApiKeyError`.

Tests do NOT call any LLM — Pydantic AI's ``Model`` constructor builds the
client lazily, so verifying the type of the returned object is a sufficient
contract test for the factory.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

from apps.api.agents.models import (
    SUPPORTED_PROVIDERS,
    MissingApiKeyError,
    UnsupportedModelError,
    build_model,
    parse_model_string,
)
from apps.api.config import Settings

_TEST_ENV = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "postgres://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CLERK_SECRET_KEY": "sk_test_fake",
    "CLERK_PUBLISHABLE_KEY": "pk_test_fake",
    "GEMINI_API_KEY": "gemini-fake-key",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-fake",
    "LANGFUSE_SECRET_KEY": "sk-lf-fake",
}


@pytest.fixture
def base_settings() -> Settings:
    """Minimal Settings with only the required fields populated.

    The ``model_copy(update=...)`` call explicitly nulls the optional
    Anthropic / OpenAI keys, since pydantic-settings reads from the
    real process environment and a developer's shell may have those
    keys set (CI does too). Without this clamp, the
    ``MissingApiKeyError`` branches would never fire under test.
    """

    with patch.dict(os.environ, _TEST_ENV, clear=False):
        s = Settings()
    return s.model_copy(
        update={"anthropic_api_key": None, "openai_api_key": None}
    )


@pytest.fixture
def settings_with_anthropic(base_settings: Settings) -> Settings:
    """Settings with an Anthropic key added."""

    return base_settings.model_copy(
        update={"anthropic_api_key": SecretStr("anthropic-fake-key")}
    )


@pytest.fixture
def settings_with_openai(base_settings: Settings) -> Settings:
    """Settings with an OpenAI key added."""

    return base_settings.model_copy(
        update={"openai_api_key": SecretStr("openai-fake-key")}
    )


# ── parse_model_string ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "valid_string,expected",
    [
        ("google-gla:gemini-2.5-flash-lite", ("google-gla", "gemini-2.5-flash-lite")),
        ("anthropic:claude-sonnet-4-6", ("anthropic", "claude-sonnet-4-6")),
        ("openai:gpt-4o-mini", ("openai", "gpt-4o-mini")),
        ("google-gla: gemini-2.5-pro ", ("google-gla", "gemini-2.5-pro")),
    ],
)
def test_parse_model_string_accepts_valid(valid_string: str, expected: tuple) -> None:
    assert parse_model_string(valid_string) == expected


@pytest.mark.parametrize(
    "bad_string",
    [
        "gemini-2.5-flash-lite",       # missing colon
        "vertex:gemini-2.5",           # unknown provider
        "google-gla:",                 # empty model name
        "",                            # empty
        ":model-only",                 # empty provider
    ],
)
def test_parse_model_string_rejects_invalid(bad_string: str) -> None:
    with pytest.raises(UnsupportedModelError):
        parse_model_string(bad_string)


def test_supported_providers_set_is_stable() -> None:
    """The factory's contract — these three providers ship in v1."""

    assert frozenset({"google-gla", "anthropic", "openai"}) == SUPPORTED_PROVIDERS


# ── build_model: per-provider branches ──────────────────────────────────────


def test_build_model_google_gla(base_settings: Settings) -> None:
    model = build_model("google-gla:gemini-2.5-flash-lite", base_settings)
    assert isinstance(model, GoogleModel)


def test_build_model_anthropic(settings_with_anthropic: Settings) -> None:
    model = build_model("anthropic:claude-sonnet-4-6", settings_with_anthropic)
    assert isinstance(model, AnthropicModel)


def test_build_model_openai(settings_with_openai: Settings) -> None:
    model = build_model("openai:gpt-4o-mini", settings_with_openai)
    assert isinstance(model, OpenAIChatModel)


def test_build_model_anthropic_missing_key_raises(base_settings: Settings) -> None:
    """Optional providers fail fast with a named error when their key is unset."""

    with pytest.raises(MissingApiKeyError, match="anthropic_api_key"):
        build_model("anthropic:claude-sonnet-4-6", base_settings)


def test_build_model_openai_missing_key_raises(base_settings: Settings) -> None:
    with pytest.raises(MissingApiKeyError, match="openai_api_key"):
        build_model("openai:gpt-4o-mini", base_settings)


def test_build_model_unknown_provider_raises(base_settings: Settings) -> None:
    with pytest.raises(UnsupportedModelError):
        build_model("vertex:gemini-2.5", base_settings)


# ── Subprocess-leakage closure ───────────────────────────────────────────────


def test_build_model_does_not_write_google_api_key_env(
    base_settings: Settings,
) -> None:
    """PLAN.md chunk 4.3a explicit assertion.

    After ``build_model("google-gla:...")`` returns, the process must NOT
    have a ``GOOGLE_API_KEY`` env var. This closes the subprocess-leakage
    posture from the previous lifespan implementation.
    """

    # Snapshot + clear so we can verify the factory doesn't add it.
    original = os.environ.pop("GOOGLE_API_KEY", None)
    try:
        model = build_model("google-gla:gemini-2.5-flash-lite", base_settings)
        assert isinstance(model, GoogleModel)
        assert os.environ.get("GOOGLE_API_KEY") is None, (
            "build_model leaked the API key into os.environ — subprocesses would "
            "inherit it. This is the regression chunk 4.3a was designed to prevent."
        )
    finally:
        if original is not None:
            os.environ["GOOGLE_API_KEY"] = original


def test_build_model_does_not_write_anthropic_api_key_env(
    settings_with_anthropic: Settings,
) -> None:
    """Same subprocess-leakage assertion for the Anthropic branch."""

    original = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        model = build_model("anthropic:claude-sonnet-4-6", settings_with_anthropic)
        assert isinstance(model, AnthropicModel)
        assert os.environ.get("ANTHROPIC_API_KEY") is None
    finally:
        if original is not None:
            os.environ["ANTHROPIC_API_KEY"] = original


def test_build_model_does_not_write_openai_api_key_env(
    settings_with_openai: Settings,
) -> None:
    """Same subprocess-leakage assertion for the OpenAI branch."""

    original = os.environ.pop("OPENAI_API_KEY", None)
    try:
        model = build_model("openai:gpt-4o-mini", settings_with_openai)
        assert isinstance(model, OpenAIChatModel)
        assert os.environ.get("OPENAI_API_KEY") is None
    finally:
        if original is not None:
            os.environ["OPENAI_API_KEY"] = original
