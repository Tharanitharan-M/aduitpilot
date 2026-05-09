-- Migration: 0010_drift_events
-- Purpose:   Sprint 9 chunks 9.5, 9.4, 9.9.
--            1. drift_events     — persisted drift events (one per detected change)
--            2. monitored_controls — per-user opt-in/out for drift watching
--            3. drift_snapshots  — last-known projection per (user, control_id)
--                                  needed by the 2-scan flap protector
--
-- Idempotent: yes — every statement uses IF NOT EXISTS / DO $$ blocks.
-- Refs: PLAN.md Sprint 9 chunks 9.4, 9.5, 9.9; ADR-0005, ADR-0008, ADR-0013;
--       system-design.md 13.

-- ── drift_events ────────────────────────────────────────────────────────────
-- Each row is one detected drift event. The dashboard renders these as
-- DriftEventCard. PATCH /api/drift/events/{id} flips status open -> dismissed
-- or open -> resolved. Re-fire is suppressed by content_hash so a previously
-- dismissed event does not re-appear unless the underlying configuration
-- actually changes again.

CREATE TABLE IF NOT EXISTS drift_events (
    id                UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id           TEXT         NOT NULL,
    control_id        TEXT         NOT NULL,
    event_type        TEXT         NOT NULL
        CONSTRAINT drift_events__type_chk
            CHECK (event_type IN (
                'status_changed', 'config_changed', 'evidence_removed'
            )),
    what_changed      TEXT         NOT NULL DEFAULT '',
    previous_value    JSONB        NOT NULL DEFAULT '{}',
    current_value     JSONB        NOT NULL DEFAULT '{}',
    suggested_fix     TEXT         NOT NULL DEFAULT '',
    source_link       TEXT,
    severity          TEXT         NOT NULL DEFAULT 'medium'
        CONSTRAINT drift_events__sev_chk
            CHECK (severity IN ('low', 'medium', 'high')),
    status            TEXT         NOT NULL DEFAULT 'open'
        CONSTRAINT drift_events__status_chk
            CHECK (status IN ('open', 'resolved', 'dismissed')),
    content_hash      TEXT         NOT NULL DEFAULT '',
    dismissed_reason  TEXT,
    detected_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_drift_events__user_status_detected
    ON drift_events (user_id, status, detected_at DESC);

CREATE INDEX IF NOT EXISTS ix_drift_events__user_control
    ON drift_events (user_id, control_id, detected_at DESC);

-- Re-fire suppression: an open or dismissed event with this content_hash
-- means "do not re-create this same event."  Worker checks this before
-- INSERT.
CREATE INDEX IF NOT EXISTS ix_drift_events__user_content_hash
    ON drift_events (user_id, content_hash)
    WHERE content_hash <> '';

ALTER TABLE drift_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE drift_events FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'drift_events'
          AND policyname = 'drift_events__user_isolation'
    ) THEN
        CREATE POLICY drift_events__user_isolation
            ON drift_events
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END$$;

COMMENT ON TABLE drift_events IS
    'Per-user drift events. Rendered as DriftEventCard on the dashboard. '
    'content_hash is a SHA-256 of (event_type, control_id, current_value) '
    'used to suppress re-fire after dismissal until the underlying config '
    'actually changes again. Sprint 9 9.4-9.9; system-design 13.';


-- ── monitored_controls ──────────────────────────────────────────────────────
-- Per-user opt-in/out registry for drift watching. Default is "monitored"
-- for every TSC clause present in the curated compliance-kb-mcp mapping;
-- a row here represents an explicit opt-OUT or an opt-IN override.

CREATE TABLE IF NOT EXISTS monitored_controls (
    user_id      TEXT         NOT NULL,
    control_id   TEXT         NOT NULL,
    monitored    BOOLEAN      NOT NULL DEFAULT true,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, control_id)
);

CREATE INDEX IF NOT EXISTS ix_monitored_controls__user_monitored
    ON monitored_controls (user_id, monitored);

ALTER TABLE monitored_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitored_controls FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'monitored_controls'
          AND policyname = 'monitored_controls__user_isolation'
    ) THEN
        CREATE POLICY monitored_controls__user_isolation
            ON monitored_controls
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END$$;

COMMENT ON TABLE monitored_controls IS
    'Per-user opt-in/out for drift watching. A missing row means "default": '
    'monitored = true. Explicit row with monitored=false suppresses drift events '
    'for that (user_id, control_id). Sprint 9 9.5; system-design 13.6.';


-- ── drift_snapshots ─────────────────────────────────────────────────────────
-- Stores the most recent normalized projection per (user, control_id).
-- The detector needs two columns:
--   * confirmed_hash   — last hash that fired drift (the prior baseline)
--   * pending_hash     — first time we saw a new hash; only after a SECOND
--                        scan with the same pending_hash do we emit drift
--                        (system-design 13.3 flap protection)

CREATE TABLE IF NOT EXISTS drift_snapshots (
    user_id          TEXT         NOT NULL,
    control_id       TEXT         NOT NULL,
    confirmed_hash   TEXT         NOT NULL DEFAULT '',
    confirmed_value  JSONB        NOT NULL DEFAULT '{}',
    pending_hash     TEXT         NOT NULL DEFAULT '',
    pending_value    JSONB        NOT NULL DEFAULT '{}',
    pending_seen_at  TIMESTAMPTZ,
    source_link      TEXT,
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, control_id)
);

CREATE INDEX IF NOT EXISTS ix_drift_snapshots__user
    ON drift_snapshots (user_id);

ALTER TABLE drift_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE drift_snapshots FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'drift_snapshots'
          AND policyname = 'drift_snapshots__user_isolation'
    ) THEN
        CREATE POLICY drift_snapshots__user_isolation
            ON drift_snapshots
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END$$;

COMMENT ON TABLE drift_snapshots IS
    'Per-(user,control) latest confirmed projection + pending projection used by '
    'the 2-scan flap protector. The detector promotes pending_hash to '
    'confirmed_hash and emits a drift_event row only after seeing the same '
    'pending_hash on two consecutive scans. Sprint 9 9.4; system-design 13.3.';
