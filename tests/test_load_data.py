from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / "database"
if str(DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(DATABASE_DIR))

from load_data import _clean_rows


def _row(
    invoice_no: str,
    stock_code: str,
    description: str,
    quantity: str,
    invoice_date: str,
    price: str,
    customer_id: str,
    country: str,
) -> dict[str, str]:
    return {
        "invoice_no": invoice_no,
        "stock_code": stock_code,
        "description": description,
        "quantity": quantity,
        "invoice_date": invoice_date,
        "price": price,
        "customer_id": customer_id,
        "country": country,
    }


def test_clean_rows_uses_most_frequent_description() -> None:
    rows = [
        _row("100", "21275", "?", "1", "2011-01-01 10:00:00", "1.0", "17850", "United Kingdom"),
        _row("101", "21275", "ZINC TOP 2 DOOR WOODEN SHELF", "1", "2011-01-01 10:01:00", "1.0", "17850", "United Kingdom"),
        _row("102", "21275", "ZINC TOP 2 DOOR WOODEN SHELF", "1", "2011-01-01 10:02:00", "1.0", "17850", "United Kingdom"),
        _row("103", "21275", "ZINC TOP 2 DOOR WOODEN SHELF", "1", "2011-01-01 10:03:00", "1.0", "17850", "United Kingdom"),
        _row("200", "21109", "LARGE CAKE TOWEL CHOCOLATE SPOTS", "1", "2011-01-01 11:00:00", "1.0", "17850", "United Kingdom"),
        _row("201", "21109", "LARGE CAKE TOWEL CHOCOLATE SPOTS", "1", "2011-01-01 11:01:00", "1.0", "17850", "United Kingdom"),
        _row("202", "21109", "LARGE CAKE TOWEL ,CHOCOLATE SPOTS", "1", "2011-01-01 11:02:00", "1.0", "17850", "United Kingdom"),
        _row("203", "99999", "", "1", "2011-01-01 11:03:00", "1.0", "17850", "United Kingdom"),
    ]

    cleaned, report = _clean_rows(rows)
    desc_by_stock = {}
    for row in cleaned:
        desc_by_stock.setdefault(row.stock_code, row.description)

    assert desc_by_stock["21275"] == "ZINC TOP 2 DOOR WOODEN SHELF"
    assert desc_by_stock["21109"] == "LARGE CAKE TOWEL CHOCOLATE SPOTS"
    assert desc_by_stock["99999"] is None
    assert report["post_clean_duplicates_removed"] == 0


def test_clean_rows_reports_post_clean_duplicate_after_date_collapse() -> None:
    rows = [
        _row("300", "ABC", "ITEM", "1", "2011-01-01 10:00:00", "1.0", "17850", "United Kingdom"),
        _row("300", "ABC", "ITEM", "1", "2011-01-01 10:05:00", "1.0", "17850", "United Kingdom"),
    ]

    cleaned, report = _clean_rows(rows)
    assert len(cleaned) == 1
    assert report["invoice_dates_collapsed"] == 1
    assert report["post_clean_duplicates_removed"] == 1