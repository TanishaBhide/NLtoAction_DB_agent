from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / "database"
if str(DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(DATABASE_DIR))

from schema_retrieval import build_engine


CASES = [
    ("customer country", {"customers"}),
    ("invoice header", {"invoices"}),
    ("invoice line items", {"invoice_items"}),
    ("product description", {"products"}),
    ("invoice date customer", {"invoices"}),
    ("stock code item", {"products"}),
    ("line item quantity", {"invoice_items"}),
    ("customer invoice lookup", {"customers", "invoices"}),
    ("country and customers", {"customers"}),
    ("invoice status", {"invoices"}),
    ("unit price and stock code", {"invoice_items", "products"}),
    ("shipment postage line", {"products", "invoice_items"}),
    ("invoice item foreign keys", {"invoice_items"}),
    ("customer key and country", {"customers"}),
    ("product catalog table", {"products"}),
    ("invoice to customer relation", {"invoices"}),
    ("quantity and price line", {"invoice_items"}),
]


def test_schema_retrieval_hit_rates() -> None:
    engine = build_engine()
    top1_hits = 0
    top3_hits = 0

    for query, expected_tables in CASES:
        results = engine.query(query, top_k=3)
        top1 = results[0]["name"]
        top3 = {result["name"] for result in results}
        if top1 in expected_tables:
            top1_hits += 1
        if top3 & expected_tables:
            top3_hits += 1

    top1_rate = top1_hits / len(CASES)
    top3_rate = top3_hits / len(CASES)

    assert top3_rate >= 0.9
    assert top1_rate >= 0.7
