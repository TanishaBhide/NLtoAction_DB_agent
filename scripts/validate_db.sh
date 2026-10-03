#!/usr/bin/env bash
# Live PostgreSQL validation for the NL-to-Action DB agent (Weeks 1-3).
#
# Recreates a throwaway database, loads both schemas and the FULL raw CSV,
# runs every query file, runs integrity checks and negative tests, and saves
# everything to docs/validation_output.txt.
#
# Connection: standard libpq environment variables (psql and load_data.py both use them):
#   PGHOST (default localhost)  PGPORT (default 5432)  PGUSER (default postgres)  PGPASSWORD
# Optional:
#   CSV_PATH       path to database_raw.csv   (default: <repo>/database_raw.csv)
#   VALIDATION_DB  database to (re)create     (default: nl_agent_validation)
#   PYTHON         python executable          (default: python3, falls back to python)
#
# Usage:  PGPASSWORD=... ./scripts/validate_db.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_DIR="$ROOT_DIR/database"
OUT_DIR="$ROOT_DIR/docs"
OUT_FILE="$OUT_DIR/validation_output.txt"
CSV_PATH="${CSV_PATH:-$ROOT_DIR/database_raw.csv}"
DB_NAME="${VALIDATION_DB:-nl_agent_validation}"

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"
# The loader must use the validation database, not whatever DATABASE_URL points to.
unset DATABASE_URL

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

mkdir -p "$OUT_DIR"
# Send everything (stdout + stderr) to the terminal AND the output file.
exec > >(tee "$OUT_FILE") 2>&1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
section() { echo; echo "================================================================"; log "$*"; echo "================================================================"; }

PSQL=(psql -X -v ON_ERROR_STOP=1 --pset=pager=off)
FAILURES=0

section "NL-to-Action DB agent - live PostgreSQL validation"
log "repo:      $ROOT_DIR"
log "csv:       $CSV_PATH"
log "server:    $PGUSER@$PGHOST:$PGPORT"
log "database:  $DB_NAME (dropped and recreated)"
log "python:    $($PY --version 2>&1)"
log "psql:      $(psql --version)"

command -v psql >/dev/null 2>&1 || { log "ERROR: psql not found on PATH"; exit 1; }
[[ -f "$CSV_PATH" ]] || { log "ERROR: CSV not found at $CSV_PATH (set CSV_PATH)"; exit 1; }

section "1. Recreate database"
"${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME;" -c "CREATE DATABASE $DB_NAME;"
export PGDATABASE="$DB_NAME"
log "server version: $("${PSQL[@]}" -tAc 'SHOW server_version;')"

section "2. Load schemas"
for file in schema.sql agent_schema.sql; do
  log "running database/$file"
  # Hide the harmless "table does not exist, skipping" NOTICEs from DROP ... IF EXISTS
  # without hiding errors: ON_ERROR_STOP + set -e still stop the script on any failure.
  PGOPTIONS="-c client_min_messages=warning" "${PSQL[@]}" -q -f "$DB_DIR/$file"
done
"${PSQL[@]}" -c "\dt"
TABLE_COUNT="$("${PSQL[@]}" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"
log "public tables: $TABLE_COUNT (expected 12)"
if [[ "$TABLE_COUNT" != "12" ]]; then log "FAIL: expected 12 tables"; FAILURES=$((FAILURES + 1)); fi

section "3. Load the full raw dataset"
START=$(date +%s)
"$PY" "$DB_DIR/load_data.py" --csv "$CSV_PATH" --reset
log "loader finished in $(( $(date +%s) - START )) s"

section "4. Agent sample data and query files"
log "running database/agent_sample_data.sql"
"${PSQL[@]}" -q -f "$DB_DIR/agent_sample_data.sql"
log "running database/queries.sql (result rows discarded - some queries return the full 536k-row join)"
"${PSQL[@]}" -q -f "$DB_DIR/queries.sql" > /dev/null
log "queries.sql: all statements executed without error"

section "5. Row counts (all 12 tables)"
"${PSQL[@]}" -c "
SELECT 'customers' AS table_name, count(*) AS rows FROM customers
UNION ALL SELECT 'products', count(*) FROM products
UNION ALL SELECT 'invoices', count(*) FROM invoices
UNION ALL SELECT 'invoice_items', count(*) FROM invoice_items
UNION ALL SELECT 'agent_sessions', count(*) FROM agent_sessions
UNION ALL SELECT 'session_events', count(*) FROM session_events
UNION ALL SELECT 'policy_rules', count(*) FROM policy_rules
UNION ALL SELECT 'column_sensitivity', count(*) FROM column_sensitivity
UNION ALL SELECT 'policy_rule_columns', count(*) FROM policy_rule_columns
UNION ALL SELECT 'schema_lookup_log', count(*) FROM schema_lookup_log
UNION ALL SELECT 'execution_audit', count(*) FROM execution_audit
UNION ALL SELECT 'compensation_ledger', count(*) FROM compensation_ledger;"

section "6. Data checks"
"${PSQL[@]}" -c "SELECT status, count(*) AS invoices FROM invoices GROUP BY status ORDER BY status;"
"${PSQL[@]}" -c "SELECT count(*) AS guest_invoices FROM invoices WHERE customer_id IS NULL;"
"${PSQL[@]}" -c "SELECT count(*) AS products_without_description FROM products WHERE description IS NULL;"
"${PSQL[@]}" -c "SELECT stock_code, description FROM products WHERE stock_code IN ('21275', '21109') ORDER BY stock_code;"
"${PSQL[@]}" -c "SELECT i.invoice_no, i.status, ii.unit_price FROM invoices i JOIN invoice_items ii USING (invoice_no) WHERE i.status = 'adjustment' ORDER BY i.invoice_no;"

check_equals() {
  local label="$1" sql="$2" expected="$3" actual
  actual="$("${PSQL[@]}" -tAc "$sql")"
  if [[ "$actual" == "$expected" ]]; then
    log "PASS: $label = $actual"
  else
    log "FAIL: $label = '$actual' (expected '$expected')"
    FAILURES=$((FAILURES + 1))
  fi
}
check_equals "product 21275 description" "SELECT description FROM products WHERE stock_code = '21275';" "ZINC TOP  2 DOOR WOODEN SHELF"
check_equals "product 21109 description" "SELECT description FROM products WHERE stock_code = '21109';" "LARGE CAKE TOWEL CHOCOLATE SPOTS"
check_equals "orphan invoice_items" "SELECT count(*) FROM invoice_items ii LEFT JOIN invoices i USING (invoice_no) WHERE i.invoice_no IS NULL;" "0"
check_equals "negative price outside adjustments" "SELECT count(*) FROM invoice_items ii JOIN invoices i USING (invoice_no) WHERE ii.unit_price < 0 AND i.status <> 'adjustment';" "0"

section "7. Negative tests (each statement MUST be rejected)"
# Each runs inside BEGIN ... ROLLBACK so an unexpected success changes nothing.
expect_error() {
  local label="$1" expected_message="$2" sql="$3" output
  if output="$("${PSQL[@]}" -c "BEGIN; $sql ROLLBACK;" 2>&1)"; then
    log "FAIL: $label - statement was ACCEPTED but must be rejected"
    FAILURES=$((FAILURES + 1))
  elif grep -q "$expected_message" <<< "$output"; then
    log "EXPECTED ERROR ($label): $(grep -m1 'ERROR' <<< "$output")"
  elif grep -q "permission denied to set role" <<< "$output"; then
    log "SKIPPED ($label): PGUSER cannot SET ROLE; run as a superuser to test role permissions"
  else
    log "FAIL: $label - rejected with an unexpected error: $(grep -m1 'ERROR' <<< "$output")"
    FAILURES=$((FAILURES + 1))
  fi
}

expect_error "negative unit_price on a completed invoice (trigger)" \
  "negative unit_price is only allowed for adjustment invoices" \
  "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('536365', '85123A', 1, -5.00);"

# UPDATE, not INSERT: the status trigger derives status from invoice_no on INSERT,
# so an inserted 'bogus' status would be silently replaced instead of reaching the CHECK.
expect_error "invalid invoice status (CHECK chk_invoices_status)" \
  "violates check constraint \"chk_invoices_status\"" \
  "UPDATE invoices SET status = 'bogus' WHERE invoice_no = '536365';"

expect_error "zero quantity (CHECK chk_invoice_items_quantity)" \
  "violates check constraint \"chk_invoice_items_quantity\"" \
  "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('536365', '85123A', 0, 1.00);"

expect_error "line item for a non-existent invoice (FK)" \
  "violates foreign key constraint" \
  "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES ('NO_SUCH_INVOICE', '85123A', 1, 1.00);"

expect_error "dbm_writer editing the append-only audit log" \
  "permission denied for table execution_audit" \
  "SET LOCAL ROLE dbm_writer; UPDATE execution_audit SET error_message = 'tampered';"

section "Result"
if [[ "$FAILURES" -eq 0 ]]; then
  log "VALIDATION PASSED - output saved to docs/validation_output.txt"
else
  log "VALIDATION FAILED - $FAILURES check(s) failed"
  exit 1
fi