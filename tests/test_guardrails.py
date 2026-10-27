from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from database.guardrails import GuardrailEngine
from database.sql_generator import ActionPlan


ROOT = Path(__file__).resolve().parents[1]
META_PATH = ROOT / "database" / "schema_metadata.json"


class AllowClassifier:
    def predict(self, plan, context=None):
        return "ALLOW", {"ALLOW": 0.99, "CONFIRM": 0.005, "BLOCK": 0.005}


class BrokenClassifier:
    def predict(self, plan, context=None):
        raise RuntimeError("classifier test failure")


def plan(sql: str, action_type: str = "read", *, count_sql: str | None = None, blocked: bool = False) -> ActionPlan:
    return ActionPlan(
        action_type=action_type,
        sql=sql,
        tables=[],
        columns=[],
        explanation="Fixture.",
        confidence=0.9,
        count_sql=count_sql,
        blocked=blocked,
        block_reason="fixture block" if blocked else None,
    )


def engine(**kwargs) -> GuardrailEngine:
    return GuardrailEngine(META_PATH, classifier=AllowClassifier(), **kwargs)


@pytest.mark.parametrize(
    ("sql", "action_type", "expected_rule"),
    [
        ("DELETE FROM customers", "write", "WHERE_REQUIRED"),
        ("UPDATE products SET description = 'x'", "write", "WHERE_REQUIRED"),
        ("DROP TABLE invoices", "ddl", "DDL_BLOCK"),
        ("TRUNCATE TABLE invoices", "ddl", "DDL_BLOCK"),
        ("ALTER TABLE customers ADD COLUMN x TEXT", "ddl", "DDL_BLOCK"),
        ("GRANT SELECT ON customers TO dbm_reader", "ddl", "DDL_BLOCK"),
        ("SELECT country FROM customers LIMIT 1; DELETE FROM customers WHERE country = 'France'", "read", "MULTI_STATEMENT"),
        ("SELECT country FROM customers /* hidden logic */ LIMIT 1", "read", "MULTI_STATEMENT"),
        ("UPDATE customers SET country = 'France' WHERE 1 = 1", "write", "WRITE_TAUTOLOGY"),
        ("DELETE FROM invoices WHERE status = 'cancelled' OR 1 = 1", "write", "WRITE_TAUTOLOGY"),
        ("SELECT * FROM customers LIMIT 10", "read", "PII_COLUMN"),
        ("SELECT customer_id FROM customers LIMIT 10", "read", "PII_COLUMN"),
        ("SELECT customer_id FROM customers WHERE country = 'France'", "read", "PII_COLUMN"),
        ("SELECT customer_id FROM customers WHERE 1 = 1 LIMIT 10", "read", "PII_COLUMN"),
        ("UPDATE customers SET customer_id = 'new' WHERE country = 'France'", "write", "PII_COLUMN"),
        ("SELECT missing FROM customers LIMIT 1", "read", None),
        ("SELECT c.description FROM customers AS c LIMIT 1", "read", None),
        ("UPDATE customers SET country = 'France' WHERE country = 'United Kingdom'", "write", "ROW_LIMIT"),
        ("SELECT i.customer_id FROM invoices AS i LIMIT 10", "read", "PII_COLUMN"),
        ("SELECT * FROM invoices AS i JOIN customers AS c ON c.customer_id = i.customer_id LIMIT 10", "read", "PII_COLUMN"),
        ("DELETE FROM invoices WHERE invoice_no = 'x' OR 'x' = 'x'", "write", "WRITE_TAUTOLOGY"),
        ("SELECT * FROM accounts LIMIT 1", "read", None),
        ("SELECT customer_id FROM customers JOIN invoices ON customers.customer_id = invoices.customer_id LIMIT 1", "read", None),
        ("SELECT country FROM customers LIMIT 101", "read", None),
        ("SELECT country FROM customers LIMIT 10 -- hidden query text", "read", "MULTI_STATEMENT"),
        ("UPDATE invoices SET customer_id = NULL WHERE customer_id = 'x'", "write", "PII_COLUMN"),
    ],
)
def test_unsafe_and_borderline_actions(sql: str, action_type: str, expected_rule: str | None) -> None:
    result = engine().evaluate(plan(sql, action_type), row_count=150 if action_type == "write" else None)
    assert result.verdict in {"CONFIRM", "BLOCK"}
    if expected_rule:
        assert expected_rule in {rule["rule_type"] for rule in result.triggered_rules}


def test_write_above_block_threshold_is_blocked() -> None:
    result = engine().evaluate(
        plan("UPDATE products SET description = 'x' WHERE stock_code = '85123A'", "write"),
        row_count=1001,
    )
    assert result.verdict == "BLOCK"
    assert "ROW_LIMIT" in {rule["rule_type"] for rule in result.triggered_rules}


def test_missing_write_row_count_requires_confirmation() -> None:
    result = engine().evaluate(
        plan(
            "UPDATE products SET description = 'x' WHERE stock_code = '85123A'",
            "write",
            count_sql="SELECT COUNT(*) FROM products WHERE stock_code = '85123A'",
        )
    )
    assert result.verdict == "CONFIRM"
    assert result.requires_approval


def test_write_row_count_callback_is_used() -> None:
    received: list[str] = []

    def count(sql: str) -> int:
        received.append(sql)
        return 101

    result = engine(row_count_provider=count).evaluate(
        plan(
            "UPDATE products SET description = 'x' WHERE stock_code = '85123A'",
            "write",
            count_sql="SELECT COUNT(*) FROM products WHERE stock_code = '85123A'",
        )
    )
    assert received == ["SELECT COUNT(*) FROM products WHERE stock_code = '85123A'"]
    assert result.verdict == "CONFIRM"


def test_count_query_comments_fail_closed_before_callback() -> None:
    called = False

    def count(sql: str) -> int:
        nonlocal called
        called = True
        return 1

    result = engine(row_count_provider=count).evaluate(
        plan(
            "UPDATE products SET description = 'x' WHERE stock_code = '85123A'",
            "write",
            count_sql="SELECT COUNT(*) FROM products WHERE stock_code = '85123A' -- hidden",
        )
    )
    assert result.verdict == "BLOCK"
    assert not called
    assert any("comments" in reason for reason in result.reasons)


@pytest.mark.parametrize(("insert_rows", "expected"), [(1, "ALLOW"), (101, "CONFIRM"), (1001, "BLOCK")])
def test_insert_values_are_counted_from_the_ast(insert_rows: int, expected: str) -> None:
    rows = ", ".join(f"('S{index}', 'sample')" for index in range(insert_rows))
    sql = f"INSERT INTO products (stock_code, description) VALUES {rows}"

    def unexpected_count(sql: str) -> int:
        raise AssertionError("INSERT row counts must not count existing target rows.")

    result = engine(row_count_provider=unexpected_count).evaluate(
        plan(
            sql,
            "write",
            count_sql="SELECT COUNT(*) FROM products",
        )
    )
    assert result.verdict == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT country FROM customers WHERE country = 'France' LIMIT 20",
        "SELECT country, COUNT(*) FROM customers GROUP BY country LIMIT 20",
        "SELECT c.country, COUNT(i.invoice_no) FROM customers c JOIN invoices i ON i.customer_id = c.customer_id GROUP BY c.country LIMIT 20",
        "SELECT c.country, COUNT(i.invoice_no) FROM customers c JOIN invoices i ON i.customer_id = c.customer_id GROUP BY c.country LIMIT 20",
        "SELECT stock_code, SUM(quantity) FROM invoice_items GROUP BY stock_code LIMIT 5",
        "SELECT stock_code, description FROM products WHERE description ILIKE '%HEART%' LIMIT 20",
        "SELECT invoice_no, status FROM invoices WHERE status = 'cancelled' LIMIT 20",
        "SELECT invoice_no, invoice_date FROM invoices WHERE invoice_date >= DATE '2011-01-01' LIMIT 20",
        "SELECT COUNT(*) FROM invoices LIMIT 1",
        "SELECT quantity, unit_price FROM invoice_items WHERE invoice_no = '536365' LIMIT 20",
        "SELECT c.country FROM customers AS c WHERE c.country = 'France' LIMIT 20",
        "SELECT c.country FROM customers AS c WHERE c.country = 'France' ORDER BY c.country LIMIT 20",
        "WITH country_counts AS (SELECT country, COUNT(*) AS n FROM customers GROUP BY country) SELECT country, n FROM country_counts LIMIT 20",
        "SELECT COUNTRY FROM CUSTOMERS WHERE COUNTRY = 'France' LIMIT 20",
        "SELECT i.invoice_no, i.status FROM invoices i WHERE i.status = 'completed' LIMIT 20",
        "SELECT country FROM customers WHERE country = 'France' LIMIT 100",
    ],
)
def test_safe_reads_are_allowed(sql: str) -> None:
    result = engine().evaluate(plan(sql))
    assert result.verdict == "ALLOW", result.reasons


@pytest.mark.parametrize(
    ("sql", "count_sql", "row_count"),
    [
        (
            "UPDATE products SET description = 'Updated' WHERE stock_code = '85123A'",
            "SELECT COUNT(*) FROM products WHERE stock_code = '85123A'",
            1,
        ),
        (
            "DELETE FROM invoice_items WHERE invoice_no = '536365'",
            "SELECT COUNT(*) FROM invoice_items WHERE invoice_no = '536365'",
            3,
        ),
        (
            "INSERT INTO products (stock_code, description) VALUES ('X', 'New')",
            "SELECT COUNT(*) FROM products",
            1,
        ),
        (
            "UPDATE invoices SET status = 'completed' WHERE invoice_no = '536365'",
            "SELECT COUNT(*) FROM invoices WHERE invoice_no = '536365'",
            1,
        ),
    ],
)
def test_scoped_low_risk_writes_are_allowed(sql: str, count_sql: str, row_count: int) -> None:
    result = engine().evaluate(plan(sql, "write", count_sql=count_sql), row_count=row_count)
    assert result.verdict == "ALLOW", result.reasons


def test_sensitive_column_through_alias_requires_confirmation() -> None:
    result = engine().evaluate(
        plan("SELECT c.customer_id FROM customers AS c LIMIT 20")
    )
    assert result.verdict == "CONFIRM"
    assert "PII_COLUMN" in {rule["rule_type"] for rule in result.triggered_rules}
    assert "customers.customer_id" in result.triggered_rules[0]["reason"]


def test_sensitive_column_matching_is_case_insensitive() -> None:
    result = engine().evaluate(plan("SELECT CUSTOMER_ID FROM CUSTOMERS LIMIT 20"))
    assert result.verdict == "CONFIRM"
    assert "PII_COLUMN" in {rule["rule_type"] for rule in result.triggered_rules}


def test_filtered_limited_sensitive_column_is_not_escalated() -> None:
    result = engine().evaluate(
        plan(
            "SELECT c.customer_id FROM customers AS c "
            "WHERE c.country = 'France' LIMIT 20"
        )
    )
    assert result.verdict == "ALLOW"
    assert "PII_COLUMN" not in {rule["rule_type"] for rule in result.triggered_rules}


def test_aggregate_does_not_expose_sensitive_group_or_join_columns() -> None:
    result = engine().evaluate(
        plan(
            "SELECT c.country, COUNT(i.invoice_no) FROM customers c "
            "JOIN invoices i ON i.customer_id = c.customer_id "
            "GROUP BY c.country LIMIT 20"
        )
    )
    assert result.verdict == "ALLOW"
    assert "PII_COLUMN" not in {rule["rule_type"] for rule in result.triggered_rules}


def test_already_blocked_plan_passes_through_without_classifier_call() -> None:
    result = GuardrailEngine(META_PATH, classifier=BrokenClassifier()).evaluate(plan("", blocked=True))
    assert result.verdict == "BLOCK"
    assert result.risk_score == 100
    assert "fixture block" in result.reasons


def test_fail_closed_on_internal_exception() -> None:
    result = GuardrailEngine(META_PATH, classifier=BrokenClassifier()).evaluate(
        plan("SELECT country FROM customers LIMIT 1")
    )
    assert result.verdict == "BLOCK"
    assert result.risk_score == 100
    assert any("classifier test failure" in reason for reason in result.reasons)


def test_classifier_cannot_downgrade_rule_based_block() -> None:
    result = engine().evaluate(plan("DELETE FROM customers", "write"))
    assert result.verdict == "BLOCK"


def test_sensitive_column_seeds_match_operational_schema_and_metadata() -> None:
    migration = (ROOT / "database" / "agent_schema_week5.sql").read_text(encoding="utf-8")
    metadata = json.loads(META_PATH.read_text(encoding="utf-8"))
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    metadata_columns = {
        table["name"].casefold(): {column["name"].casefold() for column in table["columns"]}
        for table in metadata["tables"]
    }
    schema_columns: dict[str, set[str]] = {}
    for match in re.finditer(
        r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\);",
        schema_sql,
        re.IGNORECASE | re.DOTALL,
    ):
        table_name, body = match.groups()
        columns = set()
        for line in body.splitlines():
            if not line.startswith("    ") or line.startswith("        "):
                continue
            column = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+", line.strip())
            if column and column.group(1).upper() not in {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}:
                columns.add(column.group(1).casefold())
        schema_columns[table_name.casefold()] = columns

    seeds = re.findall(
        r"SELECT\s+'([A-Za-z_][A-Za-z0-9_]*)'\s*,\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*,\s*rule_id",
        migration,
        re.IGNORECASE,
    )
    assert seeds
    for table_name, column_name in seeds:
        assert column_name.casefold() in metadata_columns[table_name.casefold()]
        assert column_name.casefold() in schema_columns[table_name.casefold()]


def test_in_memory_store_creates_pending_approval_for_confirmation() -> None:
    from database.guardrail_store import GuardrailStore
    from database.guardrails import GuardrailResult

    store = GuardrailStore(in_memory=True)
    assessment_id = store.save_assessment(
        "test-session",
        GuardrailResult(
            verdict="CONFIRM",
            risk_score=60,
            requires_approval=True,
            reasons=["Approval required."],
        ),
    )
    assert assessment_id == 1
    assert store.assessments[0]["approval_request"]["status"] == "PENDING"
