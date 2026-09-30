# PostgreSQL Sandbox Validation

This project now has the SQL needed for a live sandbox run, but the current workspace does not have `psql` installed, so the database cannot be executed and verified from here.

## What to run locally

```bash
psql -U <user> -d <database> -f database/schema.sql
psql -U <user> -d <database> -f database/sample_data.sql
psql -U <user> -d <database> -f database/queries.sql
```

If you want to validate the agent-side schema too:

```bash
psql -U <user> -d <database> -f database/agent_schema.sql
psql -U <user> -d <database> -f database/agent_sample_data.sql
```

## Minimal proof to capture

Run these checks after loading the files:

```sql
\dt
SELECT COUNT(*) FROM customers;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM invoices;
SELECT COUNT(*) FROM invoice_items;
SELECT COUNT(*) FROM agent_sessions;
SELECT COUNT(*) FROM execution_audit;
```

Capture the `psql` terminal output or screenshot as the sandbox proof for the report.

## Why this matters

The project is not just SQL text. A live run proves that the DDL, keys, foreign keys, and sample data all work together in a real PostgreSQL environment.