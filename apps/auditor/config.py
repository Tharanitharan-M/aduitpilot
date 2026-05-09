"""AuditPilot auditor service settings.

The auditor is a separate FastAPI process — it ships in its own Cloud Run
service and reuses just enough of the API's settings (LLM keys, prompt
loader config, A2A keypair) to run the AdversarialAuditor agent. We
re-declare the small subset here instead of importing apps.api.config so
the auditor has no compile-time dependency on the api package.

Refs: PLAN.md Sprint 8 chunks 8.1 / 8.2 / 8.3.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuditorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    git_sha: str = "local"

    # ── Service URLs ─────────────────────────────────────────────────────────
    auditor_public_url: str = Field(
        default="http://localhost:8001",
        description="External base URL of THIS auditor service. Goes into the AgentCard.",
    )

    # ── A2A keypair (Sprint 8 chunk 8.3) ─────────────────────────────────────
    # 32-byte raw Ed25519 seed encoded as 64 hex chars. The api side pins
    # the matching public key in AUDITOR_AGENT_PUBKEY.
    a2a_private_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("a2a_private_key", "auditor_a2a_private_key"),
    )
    a2a_public_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("a2a_public_key", "auditor_a2a_public_key"),
    )
    # Shared secret guarding /a2a + /internal/health. Required in
    # staging/production; optional in development for the test client.
    # Service-to-service inside a VPC today; mTLS / OIDC in Sprint 11.
    a2a_shared_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("a2a_shared_secret", "auditor_shared_secret"),
    )

    # ── LLM (re-using the api's keys) ────────────────────────────────────────
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    adversarial_model: str = Field(
        default="google-gla:gemini-2.5-flash-lite",
        description="Pydantic AI model identifier for the AdversarialAuditor agent.",
    )

    # ── Budget cap ───────────────────────────────────────────────────────────
    llm_budget_cap_usd: float = Field(
        default=0.50,
        description="Per-task USD ceiling enforced via LiteLLM callback.",
    )
    mock_audit_budget_usd: float | None = Field(
        default=None,
        description="Override for mock-readiness-challenge runs only.",
    )

    # ── Langfuse (optional — falls back to local YAML) ───────────────────────
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def effective_budget_cap(self) -> float:
        return self.mock_audit_budget_usd or self.llm_budget_cap_usd


@lru_cache(maxsize=1)
def get_settings() -> AuditorSettings:
    return AuditorSettings()


__all__ = ["AuditorSettings", "get_settings"]
