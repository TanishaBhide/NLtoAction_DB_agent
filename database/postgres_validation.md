# PostgreSQL Sandbox Validation

The live proof that the schema, keys, triggers, permissions and the full dataset work together in a real PostgreSQL database is produced by one script, and its output is saved to `docs/validation_output.txt`.

## How to run

Prerequisites: PostgreSQL 14+ running locally, `psql` on your PATH, the Python venv activated (`psycopg2-binary` installed), and `database_raw.csv` in the repo root (or set `CSV_PATH`).

Windows (PowerShell, from the repo root):

```powershell
$env:PGUSER = "postgres"
$env:PGPASSWORD = "your_password"
.\scripts\validate_db.ps1
# if script execution is blocked:
powershell -ExecutionPolicy Bypass -File .\scripts\validate_db.ps1
```

Linux / macOS:

```bash
PGUSER=postgres PGPASSWORD=your_password ./scripts/validate_db.sh
```

Optional settings: `PGHOST` (default `localhost`), `PGPORT` (default `5432`), `CSV_PATH`, `VALIDATION_DB` (default `nl_agent_validation`), `PYTHON`.

The script drops and recreates the `nl_agent_validation` database only. It does not touch any other database.

## What the script does

1. Drops and recreates the validation database.
2. Runs `schema.sql` and `agent_schema.sql`, then checks that all 12 tables exist.
3. Loads the full CSV with `load_data.py --reset` and logs the load report.
4. Runs `agent_sample_data.sql` and `queries.sql`.
5. Prints row counts for all 12 tables, invoices per status, guest invoices, products with no description, and the adjustment invoices.
6. Checks that product `21275` is `ZINC TOP  2 DOOR WOODEN SHELF`, product `21109` is `LARGE CAKE TOWEL CHOCOLATE SPOTS`, and that there are no orphan line items and no negative prices outside adjustment invoices.
7. Runs five negative tests that must be rejected (each inside `BEGIN ... ROLLBACK`, so nothing changes):
   - a negative `unit_price` on a completed invoice: rejected by the `trg_invoice_items_validate_unit_price` trigger
   - `status = 'bogus'`: rejected by `chk_invoices_status` (tested with `UPDATE`, because on `INSERT` the status trigger derives the status from the invoice number and overwrites any value supplied)
   - `quantity = 0`: rejected by `chk_invoice_items_quantity`
   - a line item for a non-existent invoice: rejected by the foreign key
   - `dbm_writer` updating `execution_audit`: rejected, because the audit log is append-only for that role

The script exits with code 0 and prints `VALIDATION PASSED` only if every check passes. Any failed check or failed step exits with code 1 and prints `VALIDATION FAILED` with the reason.

## Expected results

| Check | Expected |
|---|---|
| public tables | 12 |
| customers / products / invoices / invoice_items | 4,372 / 4,070 / 25,900 / 536,639 |
| invoices by status | 22,061 completed, 3,836 cancelled, 3 adjustment |
| guest invoices | 3,710 |
| products with no description | 112 |
| negative tests | 5 x `EXPECTED ERROR` |

The full load takes about 2 minutes.

## Evidence for the report

Commit `docs/validation_output.txt` from a real run. Do not edit it by hand: it is the evidence.