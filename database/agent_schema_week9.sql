-- Week 9 extension: novelty modules (intent drift, blast-radius preview).
-- Apply after agent_schema_week8.sql. Additive and idempotent.
--
-- Both tables are 1:1 specialisations of agent_requests (shared primary key):
-- a request has at most one drift assessment and at most one blast-radius
-- preview (the one computed for the plan that was finally acted on).

CREATE TABLE IF NOT EXISTS drift_assessments (
    request_id BIGINT PRIMARY KEY,
    drift_score NUMERIC(4, 3) NOT NULL,
    drift_level VARCHAR(12) NOT NULL,
    baseline_turns SMALLINT NOT NULL,
    escalated BOOLEAN NOT NULL DEFAULT FALSE,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,

    CONSTRAINT fk_drift_assessments_request
        FOREIGN KEY (request_id)
        REFERENCES agent_requests (request_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_drift_score CHECK (drift_score BETWEEN 0 AND 1),
    CONSTRAINT chk_drift_level CHECK (drift_level IN ('NO_BASELINE', 'STABLE', 'SHIFT', 'DRIFT'))
);

CREATE TABLE IF NOT EXISTS blast_radius_previews (
    request_id BIGINT PRIMARY KEY,
    target_table VARCHAR(128) NOT NULL,
    statement_type VARCHAR(10) NOT NULL,
    direct_rows INTEGER,
    cascaded_rows INTEGER NOT NULL DEFAULT 0,
    tables_touched SMALLINT NOT NULL DEFAULT 0,
    radius_level VARCHAR(12) NOT NULL,
    predicted_failure TEXT,
    impacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    triggers JSONB NOT NULL DEFAULT '[]'::jsonb,
    spread JSONB NOT NULL DEFAULT '{}'::jsonb,
    preview_ms NUMERIC(10, 2),
    -- filled from the sandboxed execution that followed the preview
    actual_affected_rows INTEGER,
    actual_sqlstate VARCHAR(5),
    prediction_correct BOOLEAN,

    CONSTRAINT fk_blast_radius_request
        FOREIGN KEY (request_id)
        REFERENCES agent_requests (request_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_blast_radius_statement CHECK (statement_type IN ('INSERT', 'UPDATE', 'DELETE')),
    CONSTRAINT chk_blast_radius_level CHECK (radius_level IN ('CONTAINED', 'MODERATE', 'WIDE', 'UNKNOWN')),
    CONSTRAINT chk_blast_radius_rows CHECK (
        (direct_rows IS NULL OR direct_rows >= 0) AND cascaded_rows >= 0 AND tables_touched >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_drift_assessments_level ON drift_assessments (drift_level);
CREATE INDEX IF NOT EXISTS idx_blast_radius_level ON blast_radius_previews (radius_level);

GRANT SELECT, INSERT ON drift_assessments, blast_radius_previews TO dbm_writer;
GRANT SELECT ON drift_assessments, blast_radius_previews TO dbm_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON drift_assessments, blast_radius_previews TO dbm_admin;
REVOKE UPDATE, DELETE ON drift_assessments, blast_radius_previews FROM dbm_writer;
