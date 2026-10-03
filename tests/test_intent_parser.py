from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / "database"
if str(DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(DATABASE_DIR))

from intent_parser import IntentParser


@pytest.fixture()
def parser() -> IntentParser:
    return IntentParser.from_paths()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("show all customers from France", {"action_type": "read", "intent": "retrieve_records", "country": "France"}),
        ("list invoice items for invoice 536365", {"action_type": "read", "intent": "retrieve_records", "invoice_no": "536365"}),
        ("add a new customer from Germany", {"action_type": "write", "intent": "insert_record", "country": "Germany"}),
        ("update the product description for stock code 85123A", {"action_type": "write", "intent": "update_record", "stock_code": "85123A"}),
        ("drop the invoices table", {"action_type": "ddl", "intent": "drop_table", "table": "invoices"}),
        ("truncate invoice items", {"action_type": "ddl", "intent": "truncate_table", "table": "invoice_items"}),
        ("alter table customers add column email", {"action_type": "ddl", "intent": "alter_schema", "table": "customers", "column": "email"}),
        ("how many invoices did we delete last month", {"action_type": "read", "intent": "aggregate_count"}),
        ("show the set of products in invoice 536365", {"action_type": "read", "intent": "retrieve_records"}),
        ("cancel invoice 536365", {"action_type": "write", "intent": "cancel_invoice", "table": "invoices"}),
        ("delete everything", {"action_type": "write", "intent": "delete_record", "unscoped": True}),
        ("customers from France", {"action_type": "read", "intent": "retrieve_records", "country": "France"}),
        ("total revenue per country", {"action_type": "read", "intent": "aggregate_sum"}),
        ("get rid of every product", {"action_type": "write", "intent": "delete_record", "table": "products", "unscoped": True}),
        ("wipe the invoice items", {"action_type": "ddl", "intent": "truncate_table", "table": "invoice_items"}),
        ("erase all customers from France", {"action_type": "write", "intent": "delete_record", "country": "France"}),
        (
            "set the country of customer 17850 to France",
            {
                "action_type": "write",
                "intent": "update_record",
                "table": "customers",
                "set_values": {"country": "France"},
                "customer_id": "17850",
            },
        ),
        (
            "change the description of stock code 85123A to 'WHITE HEART HOLDER'",
            {
                "action_type": "write",
                "intent": "update_record",
                "table": "products",
                "set_values": {"description": "WHITE HEART HOLDER"},
                "stock_code": "85123A",
            },
        ),
        ("products that cost more than 5", {"action_type": "read", "intent": "retrieve_records", "price_gt": 5}),
        (
            "invoices between 2010-12-01 and 2010-12-31",
            {"action_type": "read", "intent": "retrieve_records", "between": ["2010-12-01", "2010-12-31"]},
        ),
        (
            "which products contain 'HEART' in the description",
            {"action_type": "read", "intent": "retrieve_records", "contains": "%heart%"},
        ),
        (
            "remove all invoices from 2010",
            {"action_type": "write", "intent": "delete_record", "year": 2010, "unscoped": False},
        ),
        (
            "invoices after 2011-06-01",
            {"action_type": "read", "intent": "retrieve_records", "date_op": ">", "date_value": "2011-06-01"},
        ),
        (
            "invoices before 2011-01-01",
            {"action_type": "read", "intent": "retrieve_records", "date_op": "<", "date_value": "2011-01-01"},
        ),
    ],
)
def test_intent_parser_cases(parser: IntentParser, text: str, expected: dict[str, object]) -> None:
    parsed = parser.parse(text, session_id="T1")
    assert parsed.action_type == expected["action_type"]
    assert parsed.intent == expected["intent"]

    if country := expected.get("country"):
        assert any(constraint["table"] == "customers" and constraint["column"] == "country" and constraint["value"] == country for constraint in parsed.constraints)

    if invoice_no := expected.get("invoice_no"):
        assert any(constraint["column"] == "invoice_no" and constraint["value"] == invoice_no for constraint in parsed.constraints)

    if table := expected.get("table"):
        assert table in parsed.target_tables

    if column := expected.get("column"):
        assert column in parsed.target_columns

    if expected.get("unscoped"):
        assert parsed.is_unscoped is True
        assert parsed.is_destructive is True

    if expected.get("unscoped") is False:
        assert parsed.is_unscoped is False

    if set_values := expected.get("set_values"):
        assert parsed.set_values == set_values

    if customer_id := expected.get("customer_id"):
        assert any(c["column"] == "customer_id" and c["value"] == customer_id for c in parsed.constraints)

    if price_gt := expected.get("price_gt"):
        assert any(c["table"] == "invoice_items" and c["column"] == "unit_price" and c["operator"] == ">" and c["value"] == price_gt for c in parsed.constraints)

    if between := expected.get("between"):
        assert any(c["column"] == "invoice_date" and c["operator"] == "between" and c["value"] == between for c in parsed.constraints)

    if contains := expected.get("contains"):
        assert any(c["column"] == "description" and c["operator"] == "ILIKE" and str(c["value"]).lower() == contains for c in parsed.constraints)

    if year := expected.get("year"):
        assert any(c["column"] == "invoice_date" and c["operator"] == "year" and c["value"] == year for c in parsed.constraints)

    if date_op := expected.get("date_op"):
        assert any(c["column"] == "invoice_date" and c["operator"] == date_op and c["value"] == expected["date_value"] for c in parsed.constraints)


def test_follow_up_session_context(parser: IntentParser) -> None:
    first = parser.parse("show customers from France", session_id="S1")
    second = parser.parse("now only the ones from Germany", session_id="S1")

    assert first.action_type == "read"
    assert second.resolved_from_context is True
    assert second.previous_request == "show customers from France"
    assert second.action_type == "read"
    assert second.intent == "retrieve_records"
    assert any(constraint["value"] == "Germany" for constraint in second.constraints)


def test_follow_up_revenue_context(parser: IntentParser) -> None:
    first = parser.parse("total revenue per country", session_id="S2")
    second = parser.parse("same but for 2011", session_id="S2")
    third = parser.parse("and sort by revenue", session_id="S2")

    assert first.intent == "aggregate_sum"
    assert second.intent == "aggregate_sum"
    assert any(c["column"] == "invoice_date" and c["operator"] == "year" and c["value"] == 2011 for c in second.constraints)
    assert third.intent == "aggregate_sum"
    assert any(o["column"] == "revenue" and o["direction"] == "desc" for o in third.order_by)


def test_follow_up_merge_customer_filter(parser: IntentParser) -> None:
    first = parser.parse("show invoices in 2011", session_id="S3")
    second = parser.parse("only for customer 17850", session_id="S3")

    assert first.intent == "retrieve_records"
    assert second.resolved_from_context is True
    assert any(c["column"] == "invoice_date" and c["operator"] == "year" and c["value"] == 2011 for c in second.constraints)
    assert any(c["column"] == "customer_id" and c["value"] == "17850" for c in second.constraints)


def test_follow_up_replaces_same_column_constraint(parser: IntentParser) -> None:
    parser.parse("show customers from France", session_id="S4")
    second = parser.parse("now only the ones from Germany", session_id="S4")

    countries = [c["value"] for c in second.constraints if c["column"] == "country"]
    assert countries == ["Germany"]


@pytest.mark.parametrize(
    "text",
    [
        "how many invoices did we delete last month",
        "how many customers did we remove this year",
        "which invoices were cancelled",
        "list the deleted invoices",
        "how many rows were updated yesterday",
    ],
)
def test_questions_about_past_changes_are_reads(parser: IntentParser, text: str) -> None:
    parsed = parser.parse(text, session_id="Q1")
    assert parsed.action_type == "read"
    assert parsed.is_destructive is False


@pytest.mark.parametrize(
    "text",
    [
        "get rid of every product",
        "wipe the invoice items",
        "empty the customers table",
        "clear out the products table",
        "purge invoices from Germany",
        "delete invoices that were cancelled",
    ],
)
def test_destructive_commands_are_never_reads(parser: IntentParser, text: str) -> None:
    parsed = parser.parse(text, session_id="D1")
    assert parsed.action_type in {"write", "ddl"}
    assert parsed.is_destructive is True


@pytest.mark.parametrize(
    ("text", "where", "set_values"),
    [
        ("change the country for customer 12494 to Spain", ("customer_id", "12494"), {"country": "Spain"}),
        ("update unit price to 3 for stock code 85123A", ("stock_code", "85123A"), {"unit_price": 3}),
        ("change customer 13047's country to Italy", ("customer_id", "13047"), {"country": "Italy"}),
        ("update invoice 536365 set status to cancelled", ("invoice_no", "536365"), {"status": "cancelled"}),
    ],
)
def test_update_rule_extracts_where_and_set_values(parser: IntentParser, text: str, where: tuple[str, str], set_values: dict) -> None:
    parsed = parser.parse(text, session_id="U1")
    assert parsed.action_type == "write"
    assert parsed.intent == "update_record"
    assert parsed.set_values == set_values
    assert any(c["column"] == where[0] and c["value"] == where[1] for c in parsed.constraints)
    # The new value must not also appear as a WHERE filter.
    for column, value in set_values.items():
        assert not any(c["column"] == column and c["value"] == value for c in parsed.constraints)


@pytest.mark.parametrize(
    ("text", "column", "operator", "value"),
    [
        ("invoice items with quantity above 100", "quantity", ">", 100),
        ("invoice items with quantity below 0", "quantity", "<", 0),
        ("show invoice items with unit price at least 10", "unit_price", ">=", 10),
        ("products priced under 1", "unit_price", "<", 1),
    ],
)
def test_numeric_comparisons_work_for_any_numeric_column(parser: IntentParser, text: str, column: str, operator: str, value: int) -> None:
    parsed = parser.parse(text, session_id="N1")
    assert any(c["table"] == "invoice_items" and c["column"] == column and c["operator"] == operator and c["value"] == value for c in parsed.constraints)


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("which invoices were cancelled", "cancelled"),
        ("show cancelled invoices", "cancelled"),
        ("list adjustment invoices", "adjustment"),
        ("show completed invoices", "completed"),
    ],
)
def test_status_filter(parser: IntentParser, text: str, status: str) -> None:
    parsed = parser.parse(text, session_id="ST1")
    assert parsed.action_type == "read"
    assert any(c["table"] == "invoices" and c["column"] == "status" and c["value"] == status for c in parsed.constraints)


def test_cancel_invoice_is_not_a_status_filter(parser: IntentParser) -> None:
    parsed = parser.parse("cancel invoice 536365", session_id="ST2")
    assert parsed.intent == "cancel_invoice"
    assert not any(c["column"] == "status" for c in parsed.constraints)


def test_aggregation_group_by(parser: IntentParser) -> None:
    revenue = parser.parse("total revenue per country", session_id="G1")
    assert revenue.aggregation["function"] == "sum"
    assert revenue.aggregation["group_by"] == ["customers.country"]

    quantity = parser.parse("total quantity per product", session_id="G2")
    assert quantity.intent == "aggregate_sum"
    assert quantity.aggregation["measure"] == "quantity"
    assert quantity.aggregation["group_by"] == ["invoice_items.stock_code"]

    count = parser.parse("how many invoices per country", session_id="G3")
    assert count.intent == "aggregate_count"
    assert count.aggregation["group_by"] == ["customers.country"]


def test_follow_up_status_without_the_word_invoice(parser: IntentParser) -> None:
    parser.parse("list invoices in 2010", session_id="F1")
    second = parser.parse("only cancelled ones", session_id="F1")
    assert second.resolved_from_context is True
    assert any(c["column"] == "invoice_date" and c["operator"] == "year" and c["value"] == 2010 for c in second.constraints)
    assert any(c["column"] == "status" and c["value"] == "cancelled" for c in second.constraints)


def test_follow_up_keeps_group_by_when_sorting(parser: IntentParser) -> None:
    parser.parse("total revenue per country", session_id="F2")
    parser.parse("same but for 2011", session_id="F2")
    third = parser.parse("and sort by revenue", session_id="F2")
    assert third.aggregation["group_by"] == ["customers.country"]
    assert any(c["column"] == "invoice_date" and c["value"] == 2011 for c in third.constraints)


def test_unknown_returns_unknown(parser: IntentParser) -> None:
    parsed = parser.parse("blorb florp", session_id="S2")
    assert parsed.action_type == "unknown"
    assert parsed.intent == "unknown"