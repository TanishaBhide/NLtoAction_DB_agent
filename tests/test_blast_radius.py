"""Week 9: blast-radius preview.

Unit tests need no database. Integration tests run read-only previews against
PostgreSQL (DATABASE_URL or the Docker demo database) and compare them with
counts queried from the live data, so they do not depend on earlier demo runs.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from database.blast_radius import BlastRadius, BlastRadiusAnalyzer, TableImpact
from database.db import DEMO_DATABASE_URL, connect


# ----------------------------------------------------------------- unit tests
def test_non_write_sql_is_reported_not_raised():
    radius = BlastRadiusAnalyzer(database_url="postgresql://nobody@127.0.0.1:1/x").preview("SELECT 1")
    assert radius.level == "UNKNOWN" and radius.error


def radius(direct, table_rows, impacts=(), kind="DELETE"):
    r = BlastRadius(kind, "invoices", direct_rows=direct, table_rows=table_rows, impacts=list(impacts))
    r.level = BlastRadiusAnalyzer().classify(r)
    return r


def test_classification_levels():
    assert radius(1, 100).level == "CONTAINED"
    assert radius(1, 100, [TableImpact("invoice_items", "cascade_delete", 3, "fk", 1)]).level == "MODERATE"
    assert radius(30, 100).level == "MODERATE"            # 30% of the table
    assert radius(60, 100).level == "WIDE"                # most of the table
    assert radius(1, 10_000, [TableImpact("a", "cascade_delete", 150, "fk", 1)]).level == "WIDE"  # > 100 rows
    assert radius(1, 100, [TableImpact("a", "cascade_delete", 1, "fk", 1),
                           TableImpact("b", "cascade_delete", 1, "fk", 2)]).level == "WIDE"     # depth 2


def test_predicted_changes_merge_direct_and_cascades():
    r = radius(1, 5, [TableImpact("invoices", "direct_delete", 1),
                      TableImpact("invoice_items", "cascade_delete", 7, "fk", 1)])
    assert r.predicted_changes() == {"invoices": {"deleted": 1}, "invoice_items": {"deleted": 7}}
    assert r.cascaded_rows == 7 and r.total_rows == 8 and r.tables_touched == 2


def test_compare_with_execution():
    r = radius(1, 5)
    r.compare(SimpleNamespace(status="committed", error=None, affected_rows=1))
    assert r.prediction_correct is True
    r = radius(1, 5)
    r.predicted_sqlstate, r.predicted_failure = "23503", "restrict"
    r.compare(SimpleNamespace(status="failed", error=SimpleNamespace(sqlstate="23503"), affected_rows=None))
    assert r.prediction_correct is True
    r = radius(1, 5)
    r.compare(SimpleNamespace(status="failed", error=SimpleNamespace(sqlstate="23503"), affected_rows=None))
    assert r.prediction_correct is False  # an integrity failure the preview missed
    r = radius(1, 5)
    r.compare(SimpleNamespace(status="failed", error=SimpleNamespace(sqlstate="P0001"), affected_rows=None))
    assert r.prediction_correct is None   # trigger logic is out of scope


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
def analyzer():
    return BlastRadiusAnalyzer(database_url=DB_URL)


@needs_db
def test_delete_invoice_predicts_cascade_to_its_line_items(analyzer):
    invoice = scalar("SELECT invoice_no FROM invoice_items GROUP BY invoice_no ORDER BY COUNT(*) DESC LIMIT 1")
    items = scalar(f"SELECT COUNT(*) FROM invoice_items WHERE invoice_no = '{invoice}'")
    r = analyzer.preview(f"DELETE FROM invoices WHERE invoice_no = '{invoice}'")
    assert r.direct_rows == 1 and not r.predicted_failure
    cascade = [i for i in r.impacts if i.effect == "cascade_delete"]
    assert cascade and cascade[0].table == "invoice_items" and cascade[0].rows == items
    assert cascade[0].via == "fk_invoice_items_invoice"


@needs_db
def test_restrict_reference_predicts_23503(analyzer):
    code = scalar("SELECT stock_code FROM invoice_items LIMIT 1")
    r = analyzer.preview(f"DELETE FROM products WHERE stock_code = '{code}'")
    assert r.predicted_sqlstate == "23503" and "fk_invoice_items_product" in r.predicted_failure


@needs_db
def test_delete_customer_predicts_set_null(analyzer):
    customer = scalar("SELECT customer_id FROM invoices WHERE customer_id IS NOT NULL LIMIT 1")
    if customer is None:
        pytest.skip("no invoice with a customer in the current data")
    invoices = scalar(f"SELECT COUNT(*) FROM invoices WHERE customer_id = '{customer}'")
    r = analyzer.preview(f"DELETE FROM customers WHERE customer_id = '{customer}'")
    assert [(i.table, i.effect, i.rows) for i in r.impacts if i.effect == "set_null"] == [
        ("invoices", "set_null", invoices)]


@needs_db
def test_insert_duplicate_and_missing_parent(analyzer):
    code = scalar("SELECT stock_code FROM products LIMIT 1")
    dup = analyzer.preview(f"INSERT INTO products (stock_code, description) VALUES ('{code}', 'x')")
    assert dup.predicted_sqlstate == "23505"
    orphan = analyzer.preview("INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) "
                              "VALUES ('no-such-invoice', 'no-such-product', 1, 1.00)")
    assert orphan.predicted_sqlstate == "23503"


@needs_db
def test_key_update_cascades_and_reports_triggers(analyzer):
    invoice = scalar("SELECT invoice_no FROM invoice_items GROUP BY invoice_no ORDER BY COUNT(*) DESC LIMIT 1")
    r = analyzer.preview(f"UPDATE invoices SET invoice_no = 'Z{invoice}' WHERE invoice_no = '{invoice}'")
    assert any(i.effect == "cascade_update" and i.table == "invoice_items" for i in r.impacts)
    assert any("trg_invoices_set_status" in t for t in r.triggers)


@needs_db
def test_preview_changes_nothing(analyzer):
    before = scalar("SELECT COUNT(*) FROM invoice_items")
    analyzer.preview("DELETE FROM invoice_items WHERE quantity > 0")
    assert scalar("SELECT COUNT(*) FROM invoice_items") == before
