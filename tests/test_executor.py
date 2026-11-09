"""Week 6: safe execution engine.

Unit tests need no database. Integration tests run against PostgreSQL
(DATABASE_URL, or the Docker demo database) and are skipped when it is
unreachable. Integration tests never leave changes behind: they roll back,
or they undo their own commit with the generated compensation SQL.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from database.db import DEMO_DATABASE_URL, connect
from database.executor import SafeExecutor, capture_error
from database.guardrails import GuardrailResult
from database.sql_generator import ActionPlan

ALLOW = GuardrailResult("ALLOW", 0.0)


def plan(action_type: str, sql: str, count_sql: str | None = None, blocked: bool = False) -> ActionPlan:
    return ActionPlan(action_type, sql, [], [], "test", 0.9, count_sql, blocked, "blocked" if blocked else None)


def item_plan(sql_set: str, stock_code: str, invoice_no: str = "536365") -> ActionPlan:
    where = f"WHERE invoice_no = '{invoice_no}' AND stock_code = '{stock_code}'"
    return plan("write", f"UPDATE invoice_items SET {sql_set} {where}", f"SELECT COUNT(*) FROM invoice_items {where}")


# ----------------------------------------------------------------- unit tests
class FakeDiag:
    def __init__(self, **values):
        self.message_primary = values.get("message_primary")
        self.message_detail = values.get("message_detail")
        self.message_hint = values.get("message_hint")
        self.constraint_name = values.get("constraint_name")
        self.table_name = values.get("table_name")
        self.column_name = values.get("column_name")


class FakePgError(Exception):
    def __init__(self, pgcode, message, **diag):
        super().__init__(message)
        self.pgcode = pgcode
        self.diag = FakeDiag(message_primary=message, **diag)


@pytest.mark.parametrize(
    ("sqlstate", "category", "retryable"),
    [
        ("42883", "syntax_or_semantic", True),
        ("42703", "syntax_or_semantic", True),
        ("22P02", "data_exception", True),
        ("23514", "integrity_violation", False),
        ("23503", "integrity_violation", False),
        ("P0001", "business_rule_trigger", False),
        ("40P01", "transaction_conflict", True),
        ("57014", "cancelled_or_timeout", False),
        ("42501", "permission", False),
    ],
)
def test_capture_error_classifies_sqlstate(sqlstate, category, retryable):
    error = capture_error(FakePgError(sqlstate, "boom", constraint_name="chk_x", table_name="t"))
    assert (error.sqlstate, error.category, error.retryable) == (sqlstate, category, retryable)
    assert error.constraint == "chk_x"
    assert "boom" in error.feedback() and "chk_x" in error.feedback()


def test_capture_error_handles_non_database_exceptions():
    error = capture_error(RuntimeError("connection refused"))
    assert error.sqlstate is None and error.category == "unknown" and not error.retryable


@pytest.mark.parametrize(
    ("the_plan", "guardrail", "approved", "fragment"),
    [
        (plan("read", "SELECT 1 LIMIT 1"), GuardrailResult("BLOCK", 90.0), False, "BLOCK"),
        (plan("read", "SELECT 1 LIMIT 1"), GuardrailResult("CONFIRM", 60.0), False, "approval"),
        (plan("read", "", blocked=True), ALLOW, False, "blocked"),
        (plan("write", "DROP TABLE invoices"), ALLOW, False, "INSERT, UPDATE or DELETE"),
        (plan("read", "DELETE FROM products WHERE stock_code = 'x'"), ALLOW, False, "SELECT"),
        (plan("read", "SELECT 1; SELECT 2"), ALLOW, False, "exactly one statement"),
        (plan("ddl", "SELECT 1"), ALLOW, False, "never executed"),
    ],
)
def test_gate_refuses_without_touching_the_database(the_plan, guardrail, approved, fragment):
    executor = SafeExecutor(audit=False, database_url="postgresql://nobody@127.0.0.1:1/none")
    result = executor.execute(the_plan, guardrail, approved=approved)
    assert result.status == "not_executed"
    assert fragment in result.reason
    assert result.tx_log == []


# ---------------------------------------------------------- integration tests
def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL") or DEMO_DATABASE_URL
    try:
        connection = connect(url, connect_timeout=2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('invoice_items') IS NOT NULL")
            ready = cursor.fetchone()[0]
        connection.close()
        return url if ready else None
    except Exception:
        return None


DB_URL = _database_url()
needs_db = pytest.mark.skipif(DB_URL is None, reason="PostgreSQL demo database is not reachable")


def scalar(sql: str):
    connection = connect(DB_URL)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def executor():
    return SafeExecutor(database_url=DB_URL, audit=False)


@needs_db
def test_read_runs_in_read_only_transaction_and_rolls_back(executor):
    result = executor.execute(plan("read", "SELECT customer_id, country FROM customers WHERE country = 'France' LIMIT 100"), ALLOW)
    assert result.status == "read_only"
    assert result.rows == [["12494", "France"]]
    statements = [step.sql for step in result.tx_log]
    assert statements[0].startswith("BEGIN") and statements[0].endswith("READ ONLY")
    assert statements[-1] == "ROLLBACK"
    assert result.transaction_id.startswith("vxid")


@needs_db
def test_read_only_sandbox_blocks_a_write_hidden_inside_a_select(executor):
    # The static gate sees a SELECT; the READ ONLY transaction still refuses the side effect.
    sneaky = plan("read", "SELECT nextval('invoice_items_invoice_item_id_seq') LIMIT 1")
    result = executor.execute(sneaky, ALLOW)
    assert result.status == "failed"
    assert result.error.sqlstate == "25006"  # read_only_sql_transaction
    assert result.tx_log[-1].sql == "ROLLBACK"


@needs_db
def test_committed_write_passes_checks_and_compensation_restores_it(executor):
    before = scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'")
    result = executor.execute(item_plan(f"quantity = {before + 1}", "84029E"), ALLOW)
    try:
        assert result.status == "committed", result.reason
        assert result.affected_rows == result.expected_rows == 1
        assert all(check.passed for check in result.checks)
        assert {"rows_match_estimate", "row_limit", "deferred_constraints"} <= {c.name for c in result.checks}
        assert [step.sql for step in result.tx_log][-1] == "COMMIT"
        assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'") == before + 1
        assert result.compensation_sql and f"quantity = {before}" in result.compensation_sql[0]
    finally:
        connection = connect(DB_URL)
        with connection, connection.cursor() as cursor:
            for statement in result.compensation_sql:
                cursor.execute(statement)
        connection.close()
    assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'") == before


@needs_db
def test_dry_run_executes_then_rolls_back(executor):
    before = scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'")
    result = executor.execute(item_plan("quantity = 99", "84029E"), ALLOW, dry_run=True)
    assert result.status == "rolled_back" and result.affected_rows == 1
    assert all(check.passed for check in result.checks)
    assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'") == before


@needs_db
def test_declined_commit_rolls_back(executor):
    before = scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'")
    result = executor.execute(item_plan("quantity = 77", "84029E"), ALLOW, commit_decider=lambda preview: False)
    assert result.status == "rolled_back" and "declined" in result.reason
    assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '84029E'") == before


@needs_db
def test_check_constraint_violation_is_captured_and_rolled_back(executor):
    result = executor.execute(item_plan("quantity = 0", "71053"), ALLOW)
    assert result.status == "failed"
    assert result.error.sqlstate == "23514"
    assert result.error.constraint == "chk_invoice_items_quantity"
    assert not result.error.retryable
    assert result.tx_log[-1].sql == "ROLLBACK"
    assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '71053'") == 6


@needs_db
def test_trigger_exception_is_captured(executor):
    result = executor.execute(item_plan("unit_price = -5", "22752"), ALLOW)
    assert result.status == "failed" and result.error.sqlstate == "P0001"
    assert result.error.category == "business_rule_trigger"


@needs_db
def test_foreign_key_violation_is_captured(executor):
    result = executor.execute(
        plan("write", "DELETE FROM products WHERE stock_code = '84406B'", "SELECT COUNT(*) FROM products WHERE stock_code = '84406B'"),
        ALLOW,
    )
    assert result.status == "failed" and result.error.sqlstate == "23503"
    assert result.error.constraint == "fk_invoice_items_product"
    assert scalar("SELECT COUNT(*) FROM products WHERE stock_code = '84406B'") == 1


@needs_db
def test_invariant_precommit_check_rolls_back_write_postgres_accepts(executor):
    result = executor.execute(item_plan("quantity = -3", "21730"), ALLOW)
    assert result.status == "rolled_back"
    failed = [check.name for check in result.checks if not check.passed]
    assert failed == ["invariant:negative_qty_only_on_cancellations"]
    assert scalar("SELECT quantity FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '21730'") == 6


@needs_db
def test_row_count_mismatch_with_estimate_rolls_back(executor):
    mismatched = item_plan("quantity = quantity", "21730")
    mismatched.count_sql = "SELECT COUNT(*) FROM invoice_items WHERE invoice_no = '536365' AND stock_code = '21730'"
    mismatched.sql = "UPDATE invoice_items SET quantity = quantity WHERE invoice_no = '536365'"
    result = executor.execute(mismatched, ALLOW)
    assert result.status in {"rolled_back", "not_executed"}
    assert scalar("SELECT COUNT(*) FROM invoice_items WHERE invoice_no = '536365'") == 7


@needs_db
def test_zero_row_write_is_not_committed(executor):
    result = executor.execute(item_plan("quantity = 5", "NOPE"), ALLOW)
    assert result.status == "rolled_back"
    assert any(check.name == "rows_affected_nonzero" and not check.passed for check in result.checks)


@needs_db
def test_confirm_requires_approval_then_executes(executor):
    confirm = GuardrailResult("CONFIRM", 60.0)
    the_plan = plan("read", "SELECT stock_code FROM products WHERE stock_code = 'POST' LIMIT 1")
    assert executor.execute(the_plan, confirm).status == "not_executed"
    assert executor.execute(the_plan, confirm, approved=True).status == "read_only"


@needs_db
def test_audit_row_is_written_even_when_transaction_rolls_back():
    executor = SafeExecutor(database_url=DB_URL, audit=True)
    result = executor.execute(item_plan("quantity = 0", "71053"), ALLOW, session_id="pytest-week6", request_text="test")
    assert result.audit_id is not None, result.audit_warning
    status = scalar(f"SELECT execution_status || ':' || sqlstate FROM execution_audit WHERE audit_id = {result.audit_id}")
    assert status == "failed:23514"
