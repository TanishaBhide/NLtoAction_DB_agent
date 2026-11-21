-- Web app: role-based views for the "Database views" tab.
-- Apply after agent_schema_week8.sql (v_dba_activity builds on v_request_audit).
-- Idempotent. Each role sees a DIFFERENT slice of the database for a different job:
--
--   view                        audience          role         answers
--   v_user_catalogue            shopper           dbm_reader   "what can I buy, what does it cost, what is popular?"
--                                                              products only, no customer or order data
--   v_customer_agent_accounts   support agent     dbm_writer   "who is this customer and do they need attention?"
--                                                              one row per customer, ids MASKED, spend/refunds/flags
--   v_dba_activity              database admin    dbm_admin    "what is the agent doing to my database?"
--                                                              the audit trail: request, exact SQL, verdicts, outcome
--   (the admin page also reads table sizes from pg_catalog and the invariant counts live)

DROP VIEW IF EXISTS v_customer_agent_orders;  -- replaced by v_customer_agent_accounts
DROP VIEW IF EXISTS v_user_catalogue;

-- Non-merchandise codes in the Online Retail data: postage, manual, discount, bad debt, carriage.
CREATE OR REPLACE VIEW v_user_catalogue AS
WITH sales AS (
    SELECT
        ii.stock_code,
        MODE() WITHIN GROUP (ORDER BY ii.unit_price) FILTER (WHERE ii.unit_price > 0 AND ii.quantity > 0) AS price,
        SUM(ii.quantity) FILTER (WHERE ii.quantity > 0) AS units_sold,
        COUNT(DISTINCT ii.invoice_no) FILTER (WHERE ii.quantity > 0) AS times_ordered
    FROM invoice_items AS ii
    GROUP BY ii.stock_code
)
SELECT
    RANK() OVER (ORDER BY s.units_sold DESC NULLS LAST) AS popularity_rank,
    p.stock_code,
    COALESCE(p.description, '(no description)') AS product,
    s.price,
    s.units_sold,
    s.times_ordered,
    CASE
        WHEN RANK() OVER (ORDER BY s.units_sold DESC NULLS LAST) <= 10 THEN 'Bestseller'
        WHEN s.times_ordered >= 10 THEN 'Popular'
        ELSE 'In stock'
    END AS label
FROM products AS p
JOIN sales AS s ON s.stock_code = p.stock_code
WHERE p.stock_code NOT IN ('POST', 'DOT', 'M', 'D', 'B', 'C2', 'S')
  AND s.units_sold > 0;

CREATE OR REPLACE VIEW v_customer_agent_accounts AS
WITH per_invoice AS (
    SELECT
        i.customer_id,
        i.invoice_no,
        i.invoice_date,
        i.status,
        SUM(ii.quantity * ii.unit_price) AS amount,
        SUM(ii.quantity) AS units
    FROM invoices AS i
    JOIN invoice_items AS ii ON ii.invoice_no = i.invoice_no
    WHERE i.customer_id IS NOT NULL
    GROUP BY i.customer_id, i.invoice_no, i.invoice_date, i.status
), latest AS (
    SELECT MAX(invoice_date) AS as_of FROM invoices
), accounts AS (
    SELECT
        c.customer_id,
        c.country,
        COUNT(*) FILTER (WHERE pi.status = 'completed') AS orders,
        COUNT(*) FILTER (WHERE pi.status = 'cancelled') AS cancellations,
        COALESCE(SUM(pi.units) FILTER (WHERE pi.status = 'completed'), 0) AS units_bought,
        ROUND(COALESCE(SUM(pi.amount) FILTER (WHERE pi.status = 'completed'), 0), 2) AS total_spend,
        ROUND(COALESCE(-SUM(pi.amount) FILTER (WHERE pi.status = 'cancelled'), 0), 2) AS refunded,
        MAX(pi.invoice_date)::date AS last_order,
        (SELECT as_of::date FROM latest) - MAX(pi.invoice_date)::date AS days_since_last_order
    FROM customers AS c
    JOIN per_invoice AS pi ON pi.customer_id = c.customer_id
    GROUP BY c.customer_id, c.country
)
SELECT
    REPEAT('*', GREATEST(LENGTH(customer_id) - 2, 0)) || RIGHT(customer_id, 2) AS customer,
    country,
    orders,
    cancellations,
    units_bought,
    total_spend,
    refunded,
    last_order,
    days_since_last_order,
    CASE
        WHEN cancellations > 0 AND refunded > 0.2 * NULLIF(total_spend, 0) THEN 'Frequent refunds'
        WHEN cancellations > 0 THEN 'Has cancellations'
        WHEN total_spend >= 1000 AND days_since_last_order > 180 THEN 'VIP at risk'
        WHEN total_spend >= 1000 THEN 'VIP'
        WHEN days_since_last_order > 180 THEN 'At risk (inactive)'
        ELSE 'OK'
    END AS support_flag
FROM accounts;

CREATE OR REPLACE VIEW v_dba_activity AS
SELECT
    request_id,
    created_at,
    session_id,
    request_text,
    action_type,
    final_sql,
    guardrail_verdict,
    effective_verdict,
    final_status,
    stop_reason,
    attempts,
    last_sqlstate,
    root_cause_code
FROM v_request_audit;

GRANT SELECT ON v_user_catalogue TO dbm_reader, dbm_writer, dbm_admin;
GRANT SELECT ON v_customer_agent_accounts TO dbm_writer, dbm_admin;
GRANT SELECT ON v_dba_activity TO dbm_admin;
