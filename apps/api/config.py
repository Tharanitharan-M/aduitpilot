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

from pydantic import AliasChoices, Field, SecretStr
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
    # SecretStr because the URL embeds user:password — see Sprint 3 day-0
    # chunk 3.0c. Pydantic redacts SecretStr in repr() / model_dump() so the
    # password cannot leak through ValidationError messages, debug prints,
    # or PostHog `capture_exception` payloads.
    database_url: SecretStr = Field(
        ...,  # REQUIRED
        description="Postgres connection string. Format: postgres://USER:PASS@HOST/DB",
    )
    direct_url: SecretStr | None = Field(
        default=None,
        description="Direct (non-pooled) URL for Drizzle migrations. Defaults to database_url.",
    )

    # ── Redis — Upstash (ADR-0008, ADR-0010) ─────────────────────────────────
    # redis_url is the TCP connection string used by redis-py (`apps/api/jobs/`).
    # Local Docker:  redis://redis:6379/0
    # Upstash TCP:   rediss://default:<password>@<host>:6379
    # SecretStr for the same reason as database_url above.
    redis_url: SecretStr = Field(
        ...,  # REQUIRED
        description="Redis TCP connection string. Local: redis://localhost:6379/0",
    )
    # Optional Upstash HTTP REST creds. Only used in edge contexts where a TCP
    # connection is not viable. The chunk 2.10 JobQueue talks TCP via redis-py.
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: SecretStr | None = None

    # ── Clerk Auth (ADR-0008) ─────────────────────────────────────────────────
    clerk_secret_key: SecretStr = Field(
        ...,  # REQUIRED
        description="Clerk secret key for backend JWT verification. NEVER expose to browser.",
    )
    clerk_publishable_key: str = Field(
        ...,  # REQUIRED
        description=(
            "Clerk publishable key. Safe to expose to browser"
            " via NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY."
        ),
    )
    # Explicit JWKS URL and issuer override — set these in staging/prod so JWT
    # verification does not depend on string-parsing the publishable key.
    # Example: https://gentle-anteater-25.clerk.accounts.dev/.well-known/jwks.json
    clerk_jwks_url: str | None = None
    # Example: https://gentle-anteater-25.clerk.accounts.dev
    clerk_issuer_url: str | None = None

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
    # Accept either LANGFUSE_HOST (canonical) or LANGFUSE_BASE_URL (alternate).
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("langfuse_host", "langfuse_base_url"),
    )

    # ── PostHog — error tracking + server-side events (ADR-0009, ADR-0014) ──
    # Accept either POSTHOG_API_KEY (canonical backend name) or the frontend
    # token env var since it's the same project key (phc_...).
    posthog_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "posthog_api_key",
            "next_public_posthog_key",
            "next_public_posthog_project_token",
        ),
    )
    posthog_host: str = Field(
        default="https://us.i.posthog.com",
        validation_alias=AliasChoices("posthog_host", "next_public_posthog_host"),
    )

    # ── Grafana Cloud / OTel — backend metrics (ADR-0009) ────────────────────
    # OTel SDK reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS by convention; we
    # also accept the shorter OTLP_* alias for forward-compatible naming.
    otlp_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("otlp_endpoint", "otel_exporter_otlp_endpoint"),
    )
    otlp_headers: str | None = Field(
        default=None,
        validation_alias=AliasChoices("otlp_headers", "otel_exporter_otlp_headers"),
    )

    # ── A2A — AdversarialAuditor (ADR-0002) ──────────────────────────────────
    auditor_url: str = "http://localhost:8001"
    auditor_a2a_public_key: str | None = None   # Ed25519 hex; set in Sprint 8
    a2a_private_key: SecretStr | None = None    # auditor service only

    # ── Cron secret (Sprints 9, 11) ──────────────────────────────────────────
    cron_secret: SecretStr | None = None        # required in staging/production

    # ── Demo account (ADR-0012, Sprint 11) ───────────────────────────────────
    demo_user_id: str | None = None             # Clerk user_id of seeded demo user

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def effective_direct_url(self) -> SecretStr:
        """Return the direct (non-pooled) DB URL, falling back to database_url.

        Returns ``SecretStr`` for the same reason ``database_url`` is a
        ``SecretStr`` — callers must explicitly call ``.get_secret_value()``
        to unwrap, which keeps the unwrap surface small and greppable.
        """
        return self.direct_url or self.database_url
