#!/usr/bin/env bash
# =============================================================================
# AuditPilot — apply every SQL migration in order.
# =============================================================================
# Used by the docker-compose `migrate` one-shot service so `make docker-up`
# brings up a database that the api can talk to immediately. Idempotent:
# each migration uses CREATE TABLE IF NOT EXISTS / DO $$ BEGIN ... IF NOT
# EXISTS ... END $$ guards, so re-applying is a no-op.
#
# Picks up every file matching apps/api/db/migrations/*.sql, applies in
# lexical order (the files are numbered 0000_*.sql .. 0009_*.sql).
#
# Reads connection params from the standard PG* env vars or DATABASE_URL.
# =============================================================================

set -euo pipefail

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/app/apps/api/db/migrations}"

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "DATABASE_URL is required" >&2
    exit 1
fi

# Wait until the database is reachable. Compose handles this via
# depends_on: condition: service_healthy, but a belt-and-braces retry
# avoids race-on-first-boot.
ATTEMPTS=0
until psql "${DATABASE_URL}" -c 'SELECT 1' >/dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [[ "${ATTEMPTS}" -ge 30 ]]; then
        echo "migrate: database not reachable after 30 attempts" >&2
        exit 1
    fi
    echo "migrate: waiting for database (attempt ${ATTEMPTS}/30)..."
    sleep 2
done

echo "migrate: applying SQL files from ${MIGRATIONS_DIR}"
for sqlfile in "${MIGRATIONS_DIR}"/*.sql; do
    if [[ ! -f "${sqlfile}" ]]; then
        continue
    fi
    name="$(basename "${sqlfile}")"
    echo "  -> ${name}"
    if ! psql "${DATABASE_URL}" --single-transaction --set ON_ERROR_STOP=1 -f "${sqlfile}"; then
        echo "migrate: ${name} failed" >&2
        exit 1
    fi
done
echo "migrate: complete."
