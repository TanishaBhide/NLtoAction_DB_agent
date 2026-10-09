
-- Run this while connected to the nl2action database.

CREATE SCHEMA IF NOT EXISTS agent;

-- 1. Rules that govern permitted database actions
CREATE TABLE IF NOT EXISTS agent.policy_rule (
    rule_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name     VARCHAR(150) NOT NULL UNIQUE,
    description   TEXT NOT NULL,
    action_type   VARCHAR(20) NOT NULL
                  CHECK (action_type IN
                         ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'ANY')),
    severity      VARCHAR(20) NOT NULL DEFAULT 'medium'
                  CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Classification of sensitive columns
CREATE TABLE IF NOT EXISTS agent.column_sensitivity (
    sensitivity_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schema_name    VARCHAR(100) NOT NULL,
    table_name     VARCHAR(100) NOT NULL,
    column_name    VARCHAR(100) NOT NULL,
    sensitivity    VARCHAR(20) NOT NULL
                   CHECK (sensitivity IN
                          ('public', 'internal', 'confidential', 'restricted')),
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (schema_name, table_name, column_name)
);

-- 3. Persistent record of each user-requested action
CREATE TABLE IF NOT EXISTS agent.action_ledger (
    action_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_text    TEXT NOT NULL,
    requested_by    VARCHAR(150) NOT NULL DEFAULT 'demo_user',
    action_type     VARCHAR(20) NOT NULL
                    CHECK (action_type IN
                           ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'UNKNOWN')),
    target_schema   VARCHAR(100),
    target_table    VARCHAR(100),
    generated_sql   TEXT,
    status          VARCHAR(25) NOT NULL DEFAULT 'received'
                    CHECK (status IN
                           ('received', 'validated', 'pending_approval',
                            'approved', 'rejected', 'executing',
                            'succeeded', 'failed', 'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMPTZ
);

-- 4. Risk evaluation for an action
CREATE TABLE IF NOT EXISTS agent.risk_assessment (
    risk_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id        BIGINT NOT NULL
                     REFERENCES agent.action_ledger(action_id),
    risk_score       NUMERIC(5,2)
                     CHECK (risk_score BETWEEN 0 AND 100),
    risk_level       VARCHAR(20) NOT NULL
                     CHECK (risk_level IN
                            ('low', 'medium', 'high', 'critical')),
    explanation      TEXT NOT NULL,
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    assessed_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 5. Record of the policy rules checked for an action
CREATE TABLE IF NOT EXISTS agent.checked_against (
    check_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    risk_id        BIGINT NOT NULL
                   REFERENCES agent.risk_assessment(risk_id),
    rule_id        BIGINT NOT NULL
                   REFERENCES agent.policy_rule(rule_id),
    check_result   VARCHAR(20) NOT NULL
                   CHECK (check_result IN
                          ('pass', 'fail', 'warning', 'not_applicable')),
    details        TEXT,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (risk_id, rule_id)
);

-- 6. Human approval requests
CREATE TABLE IF NOT EXISTS agent.approval_request (
    approval_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id       BIGINT NOT NULL
                    REFERENCES agent.action_ledger(action_id),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decision        VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (decision IN ('pending', 'approved', 'rejected')),
    decided_by      VARCHAR(150),
    decided_at      TIMESTAMPTZ,
    comments        TEXT,
    CHECK (
        (decision = 'pending' AND decided_at IS NULL)
        OR
        (decision IN ('approved', 'rejected') AND decided_at IS NOT NULL)
    )
);

-- 7. Execution history
CREATE TABLE IF NOT EXISTS agent.execution_log (
    execution_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id       BIGINT NOT NULL
                    REFERENCES agent.action_ledger(action_id),
    attempt_number  INTEGER NOT NULL DEFAULT 1
                    CHECK (attempt_number > 0),
    outcome         VARCHAR(20) NOT NULL
                    CHECK (outcome IN
                           ('success', 'failure', 'rolled_back', 'blocked')),
    rows_affected   BIGINT CHECK (rows_affected >= 0),
    duration_ms     NUMERIC(12,2) CHECK (duration_ms >= 0),
    error_message   TEXT,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (action_id, attempt_number)
);

-- 8. Failed execution and retry tracking
CREATE TABLE IF NOT EXISTS agent.error_retry (
    retry_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id      BIGINT NOT NULL
                      REFERENCES agent.execution_log(execution_id),
    retry_number      INTEGER NOT NULL CHECK (retry_number > 0),
    error_category    VARCHAR(100),
    error_details     TEXT NOT NULL,
    correction_notes  TEXT,
    retry_status      VARCHAR(20) NOT NULL DEFAULT 'pending'
                      CHECK (retry_status IN
                             ('pending', 'retried', 'abandoned', 'resolved')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (execution_id, retry_number)
);

-- 9. Records of actions intended to reverse a committed action
CREATE TABLE IF NOT EXISTS agent.compensating_action (
    compensation_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_action_id   BIGINT NOT NULL
                         REFERENCES agent.action_ledger(action_id),
    compensation_action_id BIGINT
                         REFERENCES agent.action_ledger(action_id),
    reason               TEXT NOT NULL,
    status               VARCHAR(20) NOT NULL DEFAULT 'planned'
                         CHECK (status IN
                                ('planned', 'pending_approval', 'approved',
                                 'completed', 'failed', 'cancelled')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at         TIMESTAMPTZ
);

-- Useful lookup indexes
CREATE INDEX IF NOT EXISTS idx_risk_action
    ON agent.risk_assessment(action_id);

CREATE INDEX IF NOT EXISTS idx_checked_risk
    ON agent.checked_against(risk_id);

CREATE INDEX IF NOT EXISTS idx_approval_action
    ON agent.approval_request(action_id);

CREATE INDEX IF NOT EXISTS idx_execution_action
    ON agent.execution_log(action_id);

CREATE INDEX IF NOT EXISTS idx_retry_execution
    ON agent.error_retry(execution_id);

CREATE INDEX IF NOT EXISTS idx_ledger_status
    ON agent.action_ledger(status);

CREATE INDEX IF NOT EXISTS idx_ledger_created
    ON agent.action_ledger(created_at);