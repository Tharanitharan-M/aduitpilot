# syntax=docker/dockerfile:1.7
# =============================================================================
# AuditPilot API — multi-stage Dockerfile
# =============================================================================
# Build context is the repo root (see docker-compose.yml `context: .`), so
# paths are relative to /path/to/auditpilot/.
#
# Stages:
#   base  — Python 3.12-slim with uv installed, nothing project-specific
#   deps  — resolved Python dependencies via `uv pip install -e ...`
#   dev   — hot-reload uvicorn for `docker compose up api` during development
#   prod  — minimal non-root runtime for Cloud Run (Sprint 11)
#
# Sprint 8 chunk 8.4: the api now imports apps.auditor.a2a.agent_card, so we
# install the auditor package alongside the four MCP packages and the api
# itself. Using pyproject.toml as the dependency source removes the brittle
# hand-curated `pip install` list that drifted as Sprint 7 / Sprint 8 added
# new requirements.
#
# Refs: PLAN.md chunk 2.2, ADR-0008.
# =============================================================================

# ─── base ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libpq5 \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.11

WORKDIR /app

# ─── deps ────────────────────────────────────────────────────────────────────
# Copy pyproject files first so Docker layer caching works: changes to source
# code don't invalidate the dependency layer. We install from pyproject so
# this never drifts from CI / local dev.
FROM base AS deps
# Sibling packages the api imports.
COPY packages/compliance-kb-mcp/pyproject.toml ./packages/compliance-kb-mcp/pyproject.toml
COPY packages/compliance-kb-mcp/src           ./packages/compliance-kb-mcp/src
COPY packages/compliance-kb-mcp/README.md     ./packages/compliance-kb-mcp/README.md
COPY packages/policy-template-mcp/pyproject.toml ./packages/policy-template-mcp/pyproject.toml
COPY packages/policy-template-mcp/src         ./packages/policy-template-mcp/src
COPY packages/policy-template-mcp/README.md   ./packages/policy-template-mcp/README.md
COPY packages/questionnaire-mcp/pyproject.toml ./packages/questionnaire-mcp/pyproject.toml
COPY packages/questionnaire-mcp/src           ./packages/questionnaire-mcp/src
COPY packages/questionnaire-mcp/README.md     ./packages/questionnaire-mcp/README.md
COPY packages/evidence-store-mcp/pyproject.toml ./packages/evidence-store-mcp/pyproject.toml
COPY packages/evidence-store-mcp/src          ./packages/evidence-store-mcp/src
COPY packages/evidence-store-mcp/README.md    ./packages/evidence-store-mcp/README.md
# Sprint 8 chunk 8.4: api imports apps.auditor.a2a.agent_card.
COPY apps/auditor/pyproject.toml              ./apps/auditor/pyproject.toml
COPY apps/auditor                             ./apps/auditor
# api's own pyproject.
COPY apps/api/pyproject.toml                  ./apps/api/pyproject.toml
COPY apps/api/README.md                       ./apps/api/README.md
RUN uv pip install --system --no-cache \
        -e ./packages/compliance-kb-mcp \
        -e ./packages/policy-template-mcp \
        -e ./packages/questionnaire-mcp \
        -e ./packages/evidence-store-mcp \
        -e ./apps/auditor \
        -e ./apps/api

# ─── dev ─────────────────────────────────────────────────────────────────────
FROM deps AS dev
RUN uv pip install --system --no-cache \
        'pytest>=8.0' \
        'pytest-asyncio>=0.23' \
        'pytest-cov>=5.0' \
        'ruff>=0.6' \
        'mypy>=1.10' \
        'fakeredis[lua]>=2.26' \
        'respx>=0.21'
WORKDIR /app
COPY apps/api ./apps/api
COPY packages ./packages
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/apps/api"]

# ─── prod ────────────────────────────────────────────────────────────────────
FROM base AS prod
RUN groupadd -r appuser -g 1001 \
    && useradd -r -u 1001 -g appuser -m -d /home/appuser appuser

COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

WORKDIR /app
COPY --chown=appuser:appuser apps/api      ./apps/api
COPY --chown=appuser:appuser apps/auditor  ./apps/auditor
COPY --chown=appuser:appuser packages      ./packages

USER appuser
ENV PYTHONPATH=/app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
