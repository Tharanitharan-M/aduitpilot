"""
Provider-agnostic Pydantic AI model factory
==========================================
Sprint 4 chunk 4.3a: drop the ``os.environ["GOOGLE_API_KEY"]`` lifespan
mutation in favour of an explicit ``provider:model`` factory that
constructs the matching ``Provider`` with its API key wired directly
from :class:`Settings`.

Why
---
The Sprint 2 boot path stored ``Settings.gemini_api_key`` into
``os.environ["GOOGLE_API_KEY"]`` so Pydantic AI's Google integration
could pick it up implicitly. That subprocess-leakage is exactly the
posture this project is supposed to avoid: any spawned process inherits
the secret in clear text. ``build_model()`` removes the env write and
hands the secret directly to ``GoogleGLAProvider(api_key=...)`` (and the
matching providers for Anthropic / OpenAI). Operators flip providers
via ``ORCHESTRATOR_MODEL=anthropic:claude-sonnet-4-6`` in ``.env`` —
no code changes.

LiteLLM-routed models stay deferred to Sprint 8 chunk 8.2 for the
budget-callback story.

Refs
----
- PLAN.md Sprint 4 chunk 4.3a
- ADR-0001 (LangGraph 1.x runtime + Pydantic AI agents)
- ADR-0008 (free-tier infra; Gemini default)
- system-design.md §6.4
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models import Model

# NOTE: provider-specific Pydantic AI submodules import their respective
# vendor SDKs at module-import time. A single bad version in any one of
# them (anthropic, openai, google-genai) would crash the api at boot
# even when the operator only uses Gemini. We lazy-import each submodule
# inside the matching ``build_model`` branch so unused providers can not
# poison the boot.

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.config import Settings


SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"google-gla", "anthropic", "openai"}
)


class UnsupportedModelError(ValueError):
    """Raised when ``orchestrator_model`` references an unknown provider.

    Distinct from :class:`ValueError` so callers can branch on the type
    without string-matching, and so test suites can assert the precise
    failure mode.
    """


class MissingApiKeyError(RuntimeError):
    """Raised when the requested provider has no configured API key.

    The matching ``Settings`` field is optional for non-default providers
    (Anthropic, OpenAI) so callers can swap models without reissuing all
    keys at once. Construction of that provider then fails fast with a
    clear, named error rather than a downstream 401.
    """


def parse_model_string(model_string: str) -> tuple[str, str]:
    """Split a ``provider:model_name`` string.

    Examples
    --------
    >>> parse_model_string("google-gla:gemini-2.5-flash-lite")
    ('google-gla', 'gemini-2.5-flash-lite')
    >>> parse_model_string("anthropic:claude-sonnet-4-6")
    ('anthropic', 'claude-sonnet-4-6')

    Raises
    ------
    UnsupportedModelError
        If the string lacks a colon or names an unknown provider.
    """

    if ":" not in model_string:
        raise UnsupportedModelError(
            f"orchestrator_model must be 'provider:model_name'; "
            f"got {model_string!r}. Examples: 'google-gla:gemini-2.5-flash-lite', "
            f"'anthropic:claude-sonnet-4-6', 'openai:gpt-4o-mini'."
        )
    provider, _, model_name = model_string.partition(":")
    provider = provider.strip()
    model_name = model_name.strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedModelError(
            f"Unknown provider {provider!r} in {model_string!r}. "
            f"Supported providers: {sorted(SUPPORTED_PROVIDERS)}."
        )
    if not model_name:
        raise UnsupportedModelError(
            f"Missing model name in {model_string!r}. Expected 'provider:model_name'."
        )
    return provider, model_name


def build_model(model_string: str, settings: Settings) -> Model:
    """Construct a Pydantic AI :class:`Model` from a ``provider:model_name`` string.

    The provider's API key is sourced from ``settings`` (typed
    ``SecretStr``) and handed to the matching ``Provider`` constructor
    explicitly. The function never writes to ``os.environ``; subprocesses
    spawned by the request handler do not inherit the secret.

    Parameters
    ----------
    model_string : str
        ``provider:model_name`` form. See ``SUPPORTED_PROVIDERS``.
    settings : Settings
        The application settings containing the per-provider API keys.
        ``gemini_api_key`` is required (Sprint-2 contract); other keys
        are optional and only read when the matching provider is
        requested.

    Raises
    ------
    UnsupportedModelError
        If the string is malformed or names a provider this factory
        does not support today.
    MissingApiKeyError
        If the requested provider has no API key configured. The message
        names the missing ``Settings`` field so the operator can fix
        their ``.env`` and retry.
    """

    provider_id, model_name = parse_model_string(model_string)

    if provider_id == "google-gla":
        # gemini_api_key is REQUIRED on Settings, so this branch is
        # always satisfiable in production. The defensive check keeps
        # tests honest if Settings ever becomes optional for this key.
        # We pass ``vertexai=False`` so ``GoogleProvider`` uses the
        # Generative Language API path (the same surface the deprecated
        # ``GoogleGLAProvider`` covered) regardless of any ambient
        # GOOGLE_CLOUD_PROJECT / location env vars.
        if settings.gemini_api_key is None:
            raise MissingApiKeyError(
                "google-gla provider requested but gemini_api_key is not set; "
                "set GEMINI_API_KEY in .env."
            )
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        provider = GoogleProvider(
            api_key=settings.gemini_api_key.get_secret_value(),
            vertexai=False,
        )
        return GoogleModel(model_name, provider=provider)

    if provider_id == "anthropic":
        if settings.anthropic_api_key is None:
            raise MissingApiKeyError(
                "anthropic provider requested but anthropic_api_key is not set; "
                "set ANTHROPIC_API_KEY in .env."
            )
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        return AnthropicModel(model_name, provider=provider)

    if provider_id == "openai":
        if settings.openai_api_key is None:
            raise MissingApiKeyError(
                "openai provider requested but openai_api_key is not set; "
                "set OPENAI_API_KEY in .env."
            )
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value()
        )
        return OpenAIChatModel(model_name, provider=provider)

    # Defence-in-depth: parse_model_string already rejected unknown
    # providers, so this branch is unreachable. Keep it explicit so a
    # future contributor cannot silently fall through.
    raise UnsupportedModelError(
        f"Unhandled provider {provider_id!r}; this is a bug — "
        f"parse_model_string should have rejected it."
    )


__all__ = [
    "MissingApiKeyError",
    "SUPPORTED_PROVIDERS",
    "UnsupportedModelError",
    "build_model",
    "parse_model_string",
]
