"""Pydantic AI model factory for the AdversarialAuditor.

Mirrors ``apps.api.agents.models.build_model`` but lives here so the
auditor service has no compile-time dependency on the api package.

Refs: PLAN.md Sprint 8 chunk 8.2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models import Model

if TYPE_CHECKING:
    from apps.auditor.config import AuditorSettings

SUPPORTED_PROVIDERS = frozenset({"google-gla", "anthropic", "openai", "test"})


class UnsupportedModelError(ValueError):
    pass


class MissingApiKeyError(RuntimeError):
    pass


def build_model(model_string: str, settings: AuditorSettings) -> Model | str:
    """Construct a Pydantic AI model instance for ``provider:model_name``.

    Returns the literal string ``"test"`` when the caller asks for the
    Pydantic AI test model — that is a sentinel the Agent constructor
    understands directly so we don't need to import the test stubs at
    runtime.
    """

    if ":" not in model_string:
        if model_string == "test":
            return "test"
        raise UnsupportedModelError(
            f"model identifier must be 'provider:model_name', got {model_string!r}"
        )
    provider, name = model_string.split(":", 1)
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedModelError(
            f"unknown provider {provider!r}; supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    if provider == "test":
        return "test"
    if provider == "google-gla":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        if settings.gemini_api_key is None:
            raise MissingApiKeyError("GEMINI_API_KEY required for google-gla provider")
        return GoogleModel(
            model_name=name,
            provider=GoogleProvider(api_key=settings.gemini_api_key.get_secret_value()),
        )
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        if settings.anthropic_api_key is None:
            raise MissingApiKeyError("ANTHROPIC_API_KEY required for anthropic provider")
        return AnthropicModel(
            model_name=name,
            provider=AnthropicProvider(
                api_key=settings.anthropic_api_key.get_secret_value()
            ),
        )
    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        if settings.openai_api_key is None:
            raise MissingApiKeyError("OPENAI_API_KEY required for openai provider")
        return OpenAIChatModel(
            model_name=name,
            provider=OpenAIProvider(api_key=settings.openai_api_key.get_secret_value()),
        )
    raise UnsupportedModelError(f"unhandled provider {provider!r}")


__all__ = ["MissingApiKeyError", "UnsupportedModelError", "build_model"]
