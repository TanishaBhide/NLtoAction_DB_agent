"""Offline rule-based planner used by the web app when no LLM key is set (no database needed)."""

from __future__ import annotations

import pytest

from database.guardrails import GuardrailEngine
from database.rule_planner import OfflinePlannerClient, join_path, sketch_sql
from database.sql_generator import SQLGenerator

GENERATOR = SQLGenerator(llm_client=OfflinePlannerClient())


def plan(text: str, session: str | None = None):
    return GENERATOR.generate(text, session_id=session)


@pytest.mark.parametrize(("request_text", "expected"), [
    ("show customers from France", "WHERE customers.country = 'France'"),
    ("count invoices per country", "JOIN customers ON customers.customer_id = invoices.customer_id"),
    ("how many invoices per status", "GROUP BY invoices.status"),
    ("total quantity per invoice", "SUM(invoice_items.quantity)"),
    ("show products containing lantern", "ILIKE '%LANTERN%'"),
    ("show invoices from december 2010", "invoices.invoice_date >= '2010-12-01' AND invoices.invoice_date < '2011-01-01'"),
    ("show the top 5 line items by price", "ORDER BY invoice_items.unit_price DESC LIMIT 5"),
    ("show line items for invoice 536414", "FROM invoice_items WHERE invoice_items.invoice_no = '536414'"),
])
def test_reads_are_planned_and_validated(request_text, expected):
    result = plan(request_text)
    assert not result.blocked, result.block_reason
    assert result.action_type == "read" and expected in result.sql and "LIMIT" in result.sql
    assert result.explanation.startswith("[offline rule-based planner]")


def test_update_targets_the_table_that_owns_the_column_and_counts_the_same_rows():
    result = plan("set the quantity of stock code 85123A on invoice 536365 to 7")
    assert result.sql == ("UPDATE invoice_items SET quantity = 7 WHERE invoice_items.invoice_no = '536365' "
                          "AND invoice_items.stock_code = '85123A'")
    assert result.count_sql == ("SELECT COUNT(*) FROM invoice_items WHERE invoice_items.invoice_no = '536365' "
                                "AND invoice_items.stock_code = '85123A'")


def test_delete_and_insert():
    assert plan("delete invoice 536414").sql == "DELETE FROM invoices WHERE invoices.invoice_no = '536414'"
    insert = plan("add a new product with stock code 77777 and description BLUE VASE")
    assert insert.sql == "INSERT INTO products (stock_code, description) VALUES ('77777', 'BLUE VASE')"


def test_follow_up_write_uses_the_previous_request_table():
    plan("show the line items of invoice 537967", session="follow")
    result = plan("delete them", session="follow")
    assert result.sql == "DELETE FROM invoice_items WHERE invoice_items.invoice_no = '537967'"


def test_guardrails_still_decide():
    engine = GuardrailEngine()
    assert engine.evaluate(plan("show customers")).verdict == "CONFIRM"  # raw customer ids, no filter
    assert plan("delete all customers").blocked                       # unscoped: refused before planning


def test_inexpressible_requests_are_blocked_with_a_reason():
    result = plan("add a line item to invoice 536365")
    assert result.blocked and "offline rule-based planner cannot express" in result.block_reason


def test_sketch_sql_for_refused_requests():
    assert sketch_sql("drop the invoices table", {"action_type": "ddl", "intent": "drop_table",
                                                  "target_tables": ["invoices"]}) == "DROP TABLE invoices"
    assert sketch_sql("delete all customers", {"action_type": "write", "intent": "delete_record",
                                               "target_tables": ["customers"]}) == "DELETE FROM customers"


def test_join_path_goes_through_invoices():
    assert [table for table, _ in join_path("invoice_items", "customers")] == ["invoices", "customers"]
