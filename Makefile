## ============================================================================
##  AuditPilot — local-dev Makefile
## ============================================================================
##  Single source of truth for the "one command to bring everything up"
##  developer workflow. The repo also ships a docker-compose.yml for the
##  full-stack-with-Postgres-and-Redis case (see `make docker-up`).
##
##  Common targets:
##    make help            — show this table
##    make install         — bootstrap pnpm + uv deps
##    make dev             — run FastAPI + Next.js together (Ctrl-C stops both)
##    make api             — only the FastAPI backend (port 8000)
##    make web             — only the Next.js frontend (port 3000)
##    make stop            — kill anything bound to 3000 / 8000 (or stale procs)
##    make health          — curl /health on both services
##    make verify          — pnpm typecheck + pnpm test + uv run pytest
##    make docker-up       — full stack: postgres + redis + migrate + api + auditor + web
##    make docker-down     — tear down docker-compose stack (keeps volumes)
##    make docker-clean    — tear down AND delete Postgres + Redis volumes
##    make docker-restart  — docker-down + docker-up
##    make docker-logs     — tail docker-compose logs
##    make docker-status   — `docker compose ps`
##    make db-migrate      — apply every SQL migration against the running stack
##    make db-reset        — drop + re-create + re-migrate auditpilot_dev (DEV ONLY)
##
##  Refs: PLAN.md Sprint 3 (chunks 3.1, 3.7, 3.9); ADR-0008.
## ============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Pretty banner colors. Use printf so macOS /bin/echo doesn't print \033 literally.
BLUE   := \033[0;34m
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RESET  := \033[0m

ROOT     := $(shell pwd)
API_PORT := 8000
WEB_PORT := 3000

.PHONY: help
help:
	@printf "\n$(BLUE)AuditPilot — make targets$(RESET)\n\n"
	@grep -E '^##[[:space:]]+make ' Makefile | sed -E 's/^##[[:space:]]+//'
	@printf "\n"

## ── Bootstrap ───────────────────────────────────────────────────────────────

.PHONY: install
install:
	@printf "$(BLUE)▶ pnpm install$(RESET)\n"
	@pnpm install --frozen-lockfile
	@printf "$(BLUE)▶ uv sync (apps/api)$(RESET)\n"
	@cd apps/api && uv sync
	@printf "$(GREEN)✓ deps ready$(RESET)\n"

## ── Dev: API + web together ─────────────────────────────────────────────────

# uvicorn needs to import `apps.api.main`. The project root must be on
# PYTHONPATH (Docker sets ENV PYTHONPATH=/app for the same reason). uv finds
# pyproject.toml via --project apps/api.

.PHONY: dev
dev: stop
	@printf "$(BLUE)▶ FastAPI on http://localhost:$(API_PORT)  +  Next.js on http://localhost:$(WEB_PORT)$(RESET)\n"
	@printf "$(YELLOW)  Press Ctrl-C once to stop both services.$(RESET)\n"
	@trap 'kill 0' INT TERM; \
	  ( cd $(ROOT) && PYTHONPATH=$(ROOT) uv run --project apps/api uvicorn apps.api.main:app --reload --reload-dir apps/api --port $(API_PORT) ) & \
	  ( cd apps/web && pnpm dev --port $(WEB_PORT) ) & \
	  wait

.PHONY: api
api:
	@cd $(ROOT) && PYTHONPATH=$(ROOT) uv run --project apps/api uvicorn apps.api.main:app --reload --reload-dir apps/api --port $(API_PORT)

.PHONY: web
web:
	@cd apps/web && pnpm dev --port $(WEB_PORT)

## ── Stop / health ───────────────────────────────────────────────────────────

# Kill anything actually bound to the dev ports first (most reliable on macOS).
# Then sweep stale processes by command-line pattern as a backup.
.PHONY: stop
stop:
	-@lsof -ti tcp:$(API_PORT) 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	-@lsof -ti tcp:$(WEB_PORT) 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	-@pkill -f "uvicorn .*apps.api.main" 2>/dev/null || true
	-@pkill -f "next-server"             2>/dev/null || true
	-@pkill -f "next dev"                2>/dev/null || true
	-@pkill -f "pnpm dev"                2>/dev/null || true
	@printf "$(GREEN)✓ dev processes stopped$(RESET)\n"

.PHONY: health
health:
	@printf "%-12s " "FastAPI:" ; curl -fsS http://localhost:$(API_PORT)/health 2>/dev/null && printf "\n" || printf "$(YELLOW)down$(RESET)\n"
	@printf "%-12s " "Next.js:" ; curl -fsSI http://localhost:$(WEB_PORT) >/dev/null 2>&1 && printf "$(GREEN)up$(RESET)\n" || printf "$(YELLOW)down$(RESET)\n"

## ── Verify (lint + types + tests) ───────────────────────────────────────────

.PHONY: verify
verify:
	@printf "$(BLUE)▶ pnpm typecheck (web)$(RESET)\n"
	@cd apps/web && pnpm typecheck
	@printf "$(BLUE)▶ pnpm test (web)$(RESET)\n"
	@cd apps/web && pnpm test
	@printf "$(BLUE)▶ uv run pytest (api)$(RESET)\n"
	@cd $(ROOT) && PYTHONPATH=$(ROOT) uv run --project apps/api pytest apps/api/tests/ -q
	@printf "$(GREEN)✓ verify passed$(RESET)\n"

## ── Docker (full stack including Postgres + Redis + auditor) ────────────────

.PHONY: docker-up
docker-up:
	@printf "$(BLUE)▶ docker compose up (postgres + redis + migrate + api + auditor + web)$(RESET)\n"
	docker compose up -d --build
	@printf "$(GREEN)✓ stack up. Tail logs with:  make docker-logs$(RESET)\n"
	@printf "$(BLUE)  Web:     http://localhost:$(WEB_PORT)\n"
	@printf "  API:     http://localhost:$(API_PORT)/health\n"
	@printf "  Auditor: http://localhost:8001/health$(RESET)\n"

.PHONY: docker-down
docker-down:
	docker compose down

.PHONY: docker-restart
docker-restart: docker-down docker-up

.PHONY: docker-clean
docker-clean:
	@printf "$(YELLOW)▶ tearing down stack AND deleting Postgres + Redis volumes$(RESET)\n"
	docker compose down -v

.PHONY: docker-logs
docker-logs:
	docker compose logs -f --tail=100

.PHONY: docker-status
docker-status:
	@docker compose ps

## ── DB migrations ───────────────────────────────────────────────────────────

# Re-apply every SQL migration against the running compose stack. Idempotent.
.PHONY: db-migrate
db-migrate:
	@printf "$(BLUE)▶ applying migrations against compose Postgres$(RESET)\n"
	docker compose run --rm migrate

# Drop every table the api owns. Useful when the schema gets wedged in dev.
.PHONY: db-reset
db-reset:
	@printf "$(YELLOW)▶ dropping AND re-creating the auditpilot_dev database$(RESET)\n"
	docker compose exec -T postgres psql -U postgres -c "DROP DATABASE IF EXISTS auditpilot_dev"
	docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE auditpilot_dev"
	$(MAKE) db-migrate
