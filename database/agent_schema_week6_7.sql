-- Week 6/7 extension: execution audit details and ReAct session memory.
-- Apply after database/agent_schema.sql and database/agent_schema_week5.sql.
-- Additive and idempotent: safe to re-run.

-- Week 6: richer execution audit -------------------------------------------

ALTER TABLE execution_audit
    ADD COLUMN IF NOT EXISTS sqlstate VARCHAR(5),
    ADD COLUMN IF NOT EXISTS error_category VARCHAR(40),
    ADD COLUMN IF NOT EXISTS precommit_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS transaction_log JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS attempt_no INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS duration_ms NUMERIC(10, 2);

-- Reads run in a READ ONLY transaction that is always rolled back, and a
-- dry run executes a write and then rolls it back. Both need their own status.
ALTER TABLE execution_audit
    DROP CONSTRAINT IF EXISTS chk_execution_audit_status;

ALTER TABLE execution_audit
    ADD CONSTRAINT chk_execution_audit_status
        CHECK (execution_status IN (
            'not_executed', 'committed', 'rolled_back', 'failed', 'read_only'
        ));

-- Week 7: session memory -----------------------------------------------------
-- agent_sessions.current_state (JSONB) holds the rolling memory summary and
-- session_events (weak entity keyed by session_id, event_seq) holds every
-- ReAct thought / action / observation. Both tables already exist; only an
-- index for ordered replay is added here.

CREATE INDEX IF NOT EXISTS idx_session_events_type
    ON session_events (session_id, event_type);

GRANT SELECT, INSERT ON execution_audit, compensation_ledger, session_events TO dbm_writer;
GRANT UPDATE (current_state, ended_at, session_status) ON agent_sessions TO dbm_writer;
