# syntax=docker/dockerfile:1.7
# =============================================================================
# AuditPilot Auditor — multi-stage Dockerfile (Sprint 8)
# =============================================================================
# Build context is the repo root (see docker-compose.yml `context: .`).
# Sprint 8 brings the AdversarialAuditor agent online: Pydantic AI + LiteLLM
# + Ed25519 AgentCard + JSON-RPC over A2A v1.0. Install from pyproject.toml
# instead of a hand-curated pip list so the Dockerfile never drifts.
#
# The auditor reads its system prompt from
# ``apps/api/agents/prompts/adversarial/production.yaml`` at runtime, so we
# also COPY that directory into the image.
# =============================================================================

# ── base ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.11
WORKDIR /app

# ── deps ─────────────────────────────────────────────────────────────────────
# Bring the whole apps/auditor in (pyproject.toml + source) so the editable
# install resolves both the dependency graph and the package layout.
FROM base AS deps
COPY apps/auditor                ./apps/auditor
RUN uv pip install --system --no-cache -e ./apps/auditor

# ── dev ──────────────────────────────────────────────────────────────────────
FROM deps AS dev
RUN uv pip install --system --no-cache \
        'pytest>=8.0' \
        'pytest-asyncio>=0.23' \
        'ruff>=0.6' \
        'respx>=0.21'
WORKDIR /app
COPY apps/auditor                          ./apps/auditor
# Sprint 8: prompt loader looks for
# apps/api/agents/prompts/<name>/<label>.yaml
COPY apps/api/agents/prompts               ./apps/api/agents/prompts
ENV PYTHONPATH=/app
EXPOSE 8001
CMD ["uvicorn", "apps.auditor.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload", "--reload-dir", "/app/apps/auditor"]

# ── prod ─────────────────────────────────────────────────────────────────────
FROM base AS prod
RUN groupadd -r appuser -g 1001 \
    && useradd -r -u 1001 -g appuser -m -d /home/appuser appuser
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
WORKDIR /app
COPY --chown=appuser:appuser apps/auditor              ./apps/auditor
COPY --chown=appuser:appuser apps/api/agents/prompts   ./apps/api/agents/prompts
USER appuser
ENV PYTHONPATH=/app
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1
CMD ["uvicorn", "apps.auditor.main:app", "--host", "0.0.0.0", "--port", "8001"]
