INSERT INTO agent_sessions (session_id, user_label, purpose, current_state) VALUES
    ('S1', 'demo-user', 'Week 3 intent parsing demo', '{"last_query": "show customers from France"}');

INSERT INTO session_events (session_id, event_seq, event_type, event_payload) VALUES
    ('S1', 1, 'session_started', '{"source": "cli"}'),
    ('S1', 2, 'schema_lookup', '{"query": "show customers from France", "top_k": 3}'),
    ('S1', 3, 'intent_parsed', '{"action_type": "read", "intent": "retrieve_records"}');

INSERT INTO policy_rules (rule_name, action_type, rule_description, severity, enabled, requires_confirmation, rationale) VALUES
    ('block_full_table_delete', 'write', 'Reject DELETE statements without a restrictive WHERE clause.', 'high', TRUE, TRUE, 'Prevents broad destructive writes.'),
    ('require_confirmation_for_sensitive_updates', 'write', 'Require confirmation for updates to tracked columns.', 'medium', TRUE, TRUE, 'Adds explicit approval for risky writes.');

INSERT INTO column_sensitivity (table_name, column_name, sensitivity_level, read_allowed, write_allowed, notes) VALUES
    ('customers', 'country', 'normal', TRUE, TRUE, 'Useful for demo policy checks.'),
    ('invoices', 'invoice_date', 'normal', TRUE, TRUE, 'Often used in filtering and auditing.'),
    ('invoice_items', 'unit_price', 'normal', TRUE, TRUE, 'Useful for write-risk examples.');

INSERT INTO policy_rule_columns (rule_id, sensitivity_id)
SELECT r.rule_id, s.sensitivity_id
FROM policy_rules AS r
JOIN column_sensitivity AS s
  ON (r.rule_name = 'block_full_table_delete' AND s.table_name = 'invoice_items' AND s.column_name = 'unit_price')
  OR (r.rule_name = 'require_confirmation_for_sensitive_updates' AND s.table_name = 'customers' AND s.column_name = 'country')
  OR (r.rule_name = 'require_confirmation_for_sensitive_updates' AND s.table_name = 'invoice_items' AND s.column_name = 'unit_price');

INSERT INTO schema_lookup_log (session_id, user_query, retrieved_tables, retrieved_columns, retrieval_score) VALUES
    ('S1', 'show customers from France', '["customers"]', '["customer_id", "country"]', 0.9000);

INSERT INTO execution_audit (session_id, lookup_id, intent_text, generated_sql, guardrail_verdict, execution_status, risk_score, error_message, transaction_id, affected_row_count)
SELECT
    'S1',
    l.lookup_id,
    'show customers from France',
    'SELECT customer_id, country FROM customers WHERE country = ''France'';',
    'allow',
    'not_executed',
    12.50,
    NULL,
    'demo-txn-001',
    0
FROM schema_lookup_log AS l
WHERE l.session_id = 'S1'
ORDER BY l.lookup_id DESC
LIMIT 1;

INSERT INTO compensation_ledger (audit_id, compensation_sql, compensation_status)
SELECT
    a.audit_id,
    '-- no compensation needed because the demo action was read-only',
    'not_required'
FROM execution_audit AS a
WHERE a.session_id = 'S1'
ORDER BY a.audit_id DESC
LIMIT 1;