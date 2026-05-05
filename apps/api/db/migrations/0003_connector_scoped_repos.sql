-- Migration: 0003_connector_scoped_repos
-- Purpose:   Persist the user-chosen repo scope for each GitHub connector.
--            The orchestrator reads this list at scan time; an empty list
--            refuses the scan with ScanRunValidationError. Default-deny —
--            no row is inserted automatically on connect.
-- Idempotent: yes — every statement uses IF NOT EXISTS / DO $$ blocks.
-- Refs:      PLAN.md Sprint 3.5 chunk 3.5.1, ADR-0015 (repo selection at
--            scan time), ADR-0004 (read-only-by-design), ADR-0008
--            (Neon Postgres), system-design 3.1, 6.1, US-002.

-- ── Table ──────────────────────────────────────────────────────────────────────
-- connector_id stores Clerk's external_account.id (e.g. "eac_3DHw1k...") and is
-- TEXT (not a FK) because we do not maintain a local connectors table — the
-- canonical GitHub-connector record lives in Clerk. Verified during the
-- 2026-05-04 connector-not-connected debug pass.
CREATE TABLE IF NOT EXISTS connector_scoped_repos (
    id                UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    connector_id      TEXT         NOT NULL,
    user_id           TEXT         NOT NULL,
    provider_repo_id  TEXT         NOT NULL,
    full_name         TEXT         NOT NULL,
    private           BOOLEAN      NOT NULL DEFAULT false,
    selected_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ── Idempotent uniqueness on (user_id, connector_id, provider_repo_id) ────────
-- Lets us "upsert" the user's chosen set with INSERT ... ON CONFLICT DO NOTHING,
-- then DELETE the rows that fell off the new selection — single transaction
-- per save, no read-modify-write race.
--
-- user_id is the FIRST column (not connector_id) so this index is a covering
-- superset of the orchestrator's `(user_id, connector_id)` lookup and the
-- get_me COUNT — no separate ix_ index needed. user_id is included in the
-- uniqueness key per database-reviewer C-2 (2026-05-04): even though
-- Clerk's `eac_*` ids should be globally unique, we don't want to depend
-- on a third-party uniqueness contract for our cross-tenant safety —
-- including user_id makes the constraint unambiguous regardless of any
-- future Clerk id-reuse semantics.
CREATE UNIQUE INDEX IF NOT EXISTS ux_connector_scoped_repos__user_connector_repo
    ON connector_scoped_repos (user_id, connector_id, provider_repo_id);

-- ── Row Level Security ─────────────────────────────────────────────────────────
-- ADR-0015 + system-design 4: every multi-tenant table enables RLS as
-- defense-in-depth. The application is also expected to filter by user_id
-- in the WHERE clause; RLS catches a forgotten WHERE before it leaks data.
--
-- The policy uses current_setting('app.current_user_id', true) — the trailing
-- `true` returns NULL when the setting is missing (rather than erroring),
-- which means: a request that did not call set_config sees zero rows under
-- RLS, which is the correct deny-by-default behaviour. Production deployments
-- run the app under a NOBYPASSRLS role so the policy is binding; in local dev
-- the app role typically has BYPASSRLS, so app-level WHERE remains the
-- primary filter. Cross-tenant reads are blocked under either configuration.
ALTER TABLE connector_scoped_repos ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connector_scoped_repos'
          AND policyname = 'connector_scoped_repos__user_isolation'
    ) THEN
        CREATE POLICY connector_scoped_repos__user_isolation
            ON connector_scoped_repos
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END$$;

-- ── Comment for future readers ─────────────────────────────────────────────────
COMMENT ON TABLE connector_scoped_repos IS
    'User-chosen repo scope for a GitHub connector (ADR-0015). connector_id is the Clerk external_account.id (e.g. eac_*); user_id is the Clerk user id (e.g. user_*). Default-deny: no row implies no scope and the orchestrator refuses to scan.';
