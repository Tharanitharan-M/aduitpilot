"""
AuditPilot API — Settings
=========================
Pydantic-Settings v2 configuration for the FastAPI backend.

All required variables must be present in the environment (or .env file) or
Settings() raises a clear ValidationError that names the missing fields.

Acceptance test (PLAN.md 0F.4):
    python3 -c "from apps.api.config import Settings; Settings()"
Must raise pydantic_core.ValidationError when a REQUIRED var is absent,
and succeed silently when all required vars are present.

Refs: PLAN.md chunk 0F.4, .env.example, ADR-0008, ADR-0009, ADR-0011, ADR-0012.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AnyUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from environment variables.

    Fields marked REQUIRED have no default; pydantic-settings raises
    ValidationError if they are absent.  Optional fields carry sensible
    defaults so local dev works with a minimal .env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown env vars (e.g. NEXT_PUBLIC_*)
    )

    # ── Runtime ──────────────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = Field(
        ...,  # REQUIRED
        description="Deployment environment.",
    )
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    git_sha: str = "local"

    # ── Database — Neon Postgres 16 + pgvector (ADR-0008) ────────────────────
    database_url: str = Field(
        ...,  # REQUIRED
        description="Postgres connection string. Format: postgres://USER:PASS@HOST/DB",
    )
    direct_url: str | None = Field(
        default=None,
        description="Direct (non-pooled) URL for Drizzle migrations. Defaults to database_url.",
    )

    # ── Redis — Upstash (ADR-0008, ADR-0010) ─────────────────────────────────
    redis_url: str = Field(
        ...,  # REQUIRED
        description="Redis connection string. Local: redis://localhost:6379/0",
    )
    redis_token: SecretStr | None = None  # Upstash REST token; unused in local dev

    # ── Supabase Auth (ADR-0008) ─────────────────────────────────────────────
    supabase_url: AnyUrl = Field(
        ...,  # REQUIRED
        description="Supabase project URL.",
    )
    supabase_anon_key: SecretStr = Field(
        ...,  # REQUIRED
        description="Supabase anon (public) key. Safe to expose to browser.",
    )
    supabase_service_role_key: SecretStr = Field(
        ...,  # REQUIRED
        description="Supabase service-role key. NEVER expose to browser.",
    )
    supabase_jwt_secret: SecretStr = Field(
        ...,  # REQUIRED
        description="JWT secret for verifying Supabase tokens without a round-trip.",
    )

    # ── Cloudflare R2 (ADR-0008) — optional in development ───────────────────
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket_name: str = "auditpilot-files"
    r2_public_url: str = "https://files.auditpilot.dev"

    # ── LLM — Gemini via LiteLLM (ADR-0001) ─────────────────────────────────
    gemini_api_key: SecretStr = Field(
        ...,  # REQUIRED
        description="Google Gemini API key used by LiteLLM.",
    )
    anthropic_api_key: SecretStr | None = None   # optional fallback
    openai_api_key: SecretStr | None = None      # optional fallback
    llm_budget_cap_usd: float = Field(
        default=0.50,
        description="Per-session LLM cost cap in USD (AdversarialAuditor hard limit).",
    )

    # ── Langfuse — LLM observability + prompt management (ADR-0009, ADR-0011) ─
    langfuse_public_key: str = Field(
        ...,  # REQUIRED
        description="Langfuse public key for trace ingestion.",
    )
    langfuse_secret_key: SecretStr = Field(
        ...,  # REQUIRED
        description="Langfuse secret key.",
    )
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── Sentry — error monitoring (ADR-0009) ─────────────────────────────────
    sentry_dsn: str | None = None  # optional — errors still surface in logs

    # ── Grafana Cloud / OTel — backend metrics (ADR-0009) ────────────────────
    otlp_endpoint: str | None = None
    otlp_headers: str | None = None   # "Authorization=Basic <base64>"

    # ── A2A — AdversarialAuditor (ADR-0002) ──────────────────────────────────
    auditor_url: str = "http://localhost:8001"
    auditor_a2a_public_key: str | None = None   # Ed25519 hex; set in Sprint 8
    a2a_private_key: SecretStr | None = None    # auditor service only

    # ── Cron secret (Sprints 9, 11) ──────────────────────────────────────────
    cron_secret: SecretStr | None = None        # required in staging/production

    # ── Demo account (ADR-0012, Sprint 11) ───────────────────────────────────
    demo_user_id: str | None = None             # UUID of seeded demo Supabase user

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def effective_direct_url(self) -> str:
        """Return the direct (non-pooled) DB URL, falling back to database_url."""
        return self.direct_url or self.database_url
