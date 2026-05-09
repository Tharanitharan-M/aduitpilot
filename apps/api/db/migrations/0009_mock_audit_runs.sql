-- Sprint 8 chunk 8.5 — mock_audit_runs + mock_audit_findings tables.
-- Refs: PLAN.md chunks 8.5-8.7; ADR-0002 (three-agent), ADR-0010 (job lifecycle).
--
-- One row per "run mock readiness challenge" click. The orchestrator
-- enqueues a mock_audit.run job; the worker dispatches to the
-- AdversarialAuditor over A2A v1.0 and merges findings back into these
-- two tables. The SSE bridge in routes/mock_audit.py forwards row
-- updates via pg_notify so the dashboard sees objections live.

CREATE TABLE IF NOT EXISTS mock_audit_runs (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              TEXT         NOT NULL,
    scan_run_id          UUID,                                                       -- nullable; the orchestrator may not have one
    status               TEXT         NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'dispatching', 'running', 'completed',
                          'failed', 'budget_exceeded')),
    summary              TEXT         NOT NULL DEFAULT '',
    findings_count       INTEGER      NOT NULL DEFAULT 0
        CHECK (findings_count >= 0),
    severity_max         TEXT         NOT NULL DEFAULT 'none'
        CHECK (severity_max IN ('none', 'low', 'medium', 'high', 'critical')),
    spent_usd            NUMERIC(10,6) NOT NULL DEFAULT 0,
    cap_usd              NUMERIC(10,6) NOT NULL DEFAULT 0,
    a2a_task_id          TEXT,
    report_r2_key        TEXT,
    failure_reason       TEXT,
    job_idempotency_key  TEXT         NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_mock_audit_runs__user_status_created
    ON mock_audit_runs (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_mock_audit_runs__user_id
    ON mock_audit_runs (user_id);

ALTER TABLE mock_audit_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mock_audit_runs FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'mock_audit_runs'
          AND policyname = 'mock_audit_runs__user_isolation'
    ) THEN
        CREATE POLICY mock_audit_runs__user_isolation ON mock_audit_runs
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS mock_audit_findings (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID        NOT NULL REFERENCES mock_audit_runs(id) ON DELETE CASCADE,
    user_id             TEXT        NOT NULL,
    severity            TEXT        NOT NULL
        CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    tsc_id              TEXT,
    objection           TEXT        NOT NULL,
    recommended_next_step TEXT      NOT NULL DEFAULT '',
    sequence_idx        INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_mock_audit_findings__run
    ON mock_audit_findings (run_id, sequence_idx);

CREATE INDEX IF NOT EXISTS ix_mock_audit_findings__user_run
    ON mock_audit_findings (user_id, run_id);

ALTER TABLE mock_audit_findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE mock_audit_findings FORCE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'mock_audit_findings'
          AND policyname = 'mock_audit_findings__user_isolation'
    ) THEN
        CREATE POLICY mock_audit_findings__user_isolation ON mock_audit_findings
            USING (user_id = current_setting('app.current_user_id', true))
            WITH CHECK (user_id = current_setting('app.current_user_id', true));
    END IF;
END $$;


-- LISTEN/NOTIFY bridge: every UPDATE on mock_audit_runs broadcasts a
-- compact JSON payload on the 'mock_audit_run_updates' channel so the
-- API's SSE bridge can forward it to the dashboard.
CREATE OR REPLACE FUNCTION mock_audit_runs__notify_update()
RETURNS TRIGGER AS $$
DECLARE
    payload TEXT;
BEGIN
    -- Postgres pg_notify silently truncates payloads above 8 000 bytes,
    -- which would corrupt the JSON the SSE bridge consumes. Cap the
    -- mutable strings so the worst case stays well inside the limit.
    payload := json_build_object(
        'run_id', NEW.id::text,
        'user_id', NEW.user_id,
        'status', NEW.status,
        'summary', LEFT(COALESCE(NEW.summary, ''), 500),
        'findings_count', NEW.findings_count,
        'severity_max', NEW.severity_max,
        'spent_usd', NEW.spent_usd,
        'cap_usd', NEW.cap_usd,
        'report_r2_key', NEW.report_r2_key,
        'failure_reason', LEFT(COALESCE(NEW.failure_reason, ''), 200)
    )::text;
    PERFORM pg_notify('mock_audit_run_updates', payload);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mock_audit_runs__notify_update_trigger ON mock_audit_runs;

CREATE TRIGGER mock_audit_runs__notify_update_trigger
    AFTER INSERT OR UPDATE ON mock_audit_runs
    FOR EACH ROW
    EXECUTE FUNCTION mock_audit_runs__notify_update();
