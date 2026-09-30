"""Load the Online Retail CSV into the operational PostgreSQL schema.

The loader applies the Week 1 cleaning rules in Python and inserts the cleaned
rows into the normalized tables in one database transaction.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import psycopg2
from psycopg2 import sql


RAW_COLUMNS = [
    "invoice_no",
    "stock_code",
    "description",
    "quantity",
    "invoice_date",
    "price",
    "customer_id",
    "country",
]


@dataclass(frozen=True)
class CleanedRow:
    invoice_no: str
    stock_code: str
    description: str | None
    quantity: int
    invoice_date: datetime
    price: float
    customer_id: str | None
    country: str
    status: str


def _parse_connection_url() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    user = os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER")
    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    database = os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB")
    if not (user and database):
        raise RuntimeError("DATABASE_URL or PGUSER/PGDATABASE must be set")

    auth = f"{user}:{password or ''}" if password else user
    return f"postgresql://{auth}@{host}:{port}/{database}"


def _normalize_customer_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.endswith(".0"):
        value = value[:-2]
    return value


def _parse_float(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _parse_int(value: str | None) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def _status_for_invoice(invoice_no: str) -> str:
    if invoice_no.startswith("C"):
        return "cancelled"
    if invoice_no.startswith("A"):
        return "adjustment"
    return "completed"


def _load_raw_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    "invoice_no": (row.get("InvoiceNo") or "").strip(),
                    "stock_code": (row.get("StockCode") or "").strip(),
                    "description": (row.get("Description") or "").strip(),
                    "quantity": (row.get("Quantity") or "").strip(),
                    "invoice_date": (row.get("InvoiceDate") or "").strip(),
                    "price": (row.get("Price") or "").strip(),
                    "customer_id": _normalize_customer_id(row.get("Customer ID")),
                    "country": (row.get("Country") or "").strip(),
                }
            )
        return rows


def _dedupe_exact_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    cleaned: list[dict[str, str]] = []
    removed = 0
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        cleaned.append(row)
    return cleaned, removed


def _choose_country(values: list[tuple[str, str]]) -> str:
    country_counter = Counter(country for _, country in values if country)
    if not country_counter:
        return ""
    top_count = max(country_counter.values())
    candidates = {country for country, count in country_counter.items() if count == top_count}
    if len(candidates) == 1:
        return next(iter(candidates))

    latest_country = ""
    latest_date = None
    for invoice_date, country in values:
        parsed_date = datetime.fromisoformat(invoice_date)
        if country in candidates and (latest_date is None or parsed_date > latest_date):
            latest_date = parsed_date
            latest_country = country
    return latest_country


def _choose_description(values: list[str]) -> str | None:
    filtered = [value for value in values if value]
    if not filtered:
        return None
    counts = Counter(filtered)
    return counts.most_common(1)[0][0]


def _clean_rows(raw_rows: list[dict[str, str]]) -> tuple[list[CleanedRow], dict[str, Any]]:
    deduped_rows, duplicates_removed = _dedupe_exact_rows(raw_rows)

    invoice_dates: dict[str, list[str]] = defaultdict(list)
    customer_country_history: dict[str, list[tuple[str, str]]] = defaultdict(list)
    stock_descriptions: dict[str, list[str]] = defaultdict(list)

    for row in deduped_rows:
        invoice_no = row["invoice_no"]
        invoice_dates[invoice_no].append(row["invoice_date"])
        if row["customer_id"]:
            customer_country_history[row["customer_id"]].append((row["invoice_date"], row["country"]))
        stock_descriptions[row["stock_code"]].append(row["description"])

    invoice_date_choice = {invoice_no: min(values) for invoice_no, values in invoice_dates.items()}
    customer_country_choice = {customer_id: _choose_country(values) for customer_id, values in customer_country_history.items()}
    stock_description_choice = {stock_code: _choose_description(values) for stock_code, values in stock_descriptions.items()}

    cleaned: list[CleanedRow] = []
    seen_clean_rows: set[tuple[Any, ...]] = set()
    post_clean_duplicates_removed = 0
    for row in deduped_rows:
        invoice_no = row["invoice_no"]
        status = _status_for_invoice(invoice_no)
        customer_id = row["customer_id"]
        if customer_id:
            country = customer_country_choice.get(customer_id, row["country"])
        else:
            country = row["country"] or "United Kingdom"

        description = stock_description_choice.get(row["stock_code"])
        cleaned_row = CleanedRow(
            invoice_no=invoice_no,
            stock_code=row["stock_code"],
            description=description,
            quantity=_parse_int(row["quantity"]),
            invoice_date=datetime.fromisoformat(invoice_date_choice[invoice_no]),
            price=_parse_float(row["price"]),
            customer_id=customer_id,
            country=country,
            status=status,
        )
        key = (
            cleaned_row.invoice_no,
            cleaned_row.stock_code,
            cleaned_row.description,
            cleaned_row.quantity,
            cleaned_row.invoice_date.isoformat(sep=" "),
            cleaned_row.price,
            cleaned_row.customer_id,
            cleaned_row.country,
            cleaned_row.status,
        )
        if key not in seen_clean_rows:
            seen_clean_rows.add(key)
            cleaned.append(cleaned_row)
        else:
            post_clean_duplicates_removed += 1

    report = {
        "raw_rows": len(raw_rows),
        "duplicates_removed": duplicates_removed,
        "post_clean_duplicates_removed": post_clean_duplicates_removed,
        "clean_rows": len(cleaned),
        "invoice_dates_collapsed": sum(1 for values in invoice_dates.values() if len(set(values)) > 1),
        "customer_country_conflicts": sum(1 for values in customer_country_history.values() if len({country for _, country in values if country}) > 1),
        "stockcode_multi_descriptions": sum(1 for values in stock_descriptions.values() if len({value for value in values if value}) > 1),
        "stockcode_null_description_only": sum(1 for values in stock_descriptions.values() if not any(value for value in values)),
        "guest_invoice_rows": sum(1 for row in cleaned if row.customer_id is None),
        "invoice_status_counts": Counter(row.status for row in cleaned),
        "products_null_description_rows": sum(1 for row in cleaned if row.description is None),
    }
    return cleaned, report


def _truncate_operational_tables(cursor: Any) -> None:
    cursor.execute("TRUNCATE TABLE invoice_items, invoices, products, customers RESTART IDENTITY CASCADE;")


def _operational_tables_non_empty(cursor: Any) -> dict[str, int]:
    counts = {
        "customers": _count_rows(cursor, "customers"),
        "products": _count_rows(cursor, "products"),
        "invoices": _count_rows(cursor, "invoices"),
        "invoice_items": _count_rows(cursor, "invoice_items"),
    }
    return {name: count for name, count in counts.items() if count > 0}


def _load_operational_data(cursor: Any, cleaned_rows: list[CleanedRow]) -> None:
    customers: dict[str, str] = {}
    products: dict[str, str | None] = {}
    invoices: dict[str, tuple[datetime, str | None, str]] = {}
    invoice_items: list[tuple[str, str, int, float]] = []

    for row in cleaned_rows:
        if row.customer_id is not None and row.customer_id not in customers:
            customers[row.customer_id] = row.country
        if row.stock_code not in products:
            products[row.stock_code] = row.description
        if row.invoice_no not in invoices:
            invoices[row.invoice_no] = (row.invoice_date, row.customer_id, row.status)
        invoice_items.append((row.invoice_no, row.stock_code, row.quantity, row.price))

    if customers:
        cursor.executemany(
            "INSERT INTO customers (customer_id, country) VALUES (%s, %s)",
            list(customers.items()),
        )

    if products:
        cursor.executemany(
            "INSERT INTO products (stock_code, description) VALUES (%s, %s)",
            list(products.items()),
        )

    if invoices:
        cursor.executemany(
            "INSERT INTO invoices (invoice_no, invoice_date, customer_id, status) VALUES (%s, %s, %s, %s)",
            [(invoice_no, invoice_date, customer_id, status) for invoice_no, (invoice_date, customer_id, status) in invoices.items()],
        )

    if invoice_items:
        cursor.executemany(
            "INSERT INTO invoice_items (invoice_no, stock_code, quantity, unit_price) VALUES (%s, %s, %s, %s)",
            invoice_items,
        )


def _count_rows(cursor: Any, table_name: str) -> int:
    cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table_name)))
    return int(cursor.fetchone()[0])


def _build_report(cursor: Any, report: dict[str, Any]) -> dict[str, Any]:
    cursor.execute("SELECT COUNT(*) FROM invoices WHERE customer_id IS NULL")
    guest_invoice_count = int(cursor.fetchone()[0])

    cursor.execute("SELECT COUNT(*) FROM products WHERE description IS NULL")
    products_null_description = int(cursor.fetchone()[0])

    cursor.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status ORDER BY status")
    invoice_status_counts = {status: int(count) for status, count in cursor.fetchall()}

    return {
        **report,
        "tables": {
            "customers": _count_rows(cursor, "customers"),
            "products": _count_rows(cursor, "products"),
            "invoices": _count_rows(cursor, "invoices"),
            "invoice_items": _count_rows(cursor, "invoice_items"),
        },
        "guest_invoices": guest_invoice_count,
        "products_null_description": products_null_description,
        "invoice_status_counts": invoice_status_counts,
    }


def _print_report(report: dict[str, Any]) -> None:
    print("LOAD REPORT")
    print(json.dumps(report, indent=2, default=str))


def load_database(csv_path: Path, reset: bool = False) -> dict[str, Any]:
    dsn = _parse_connection_url()
    raw_rows = _load_raw_rows(csv_path)
    cleaned_rows, cleaning_report = _clean_rows(raw_rows)

    with closing(psycopg2.connect(dsn)) as connection:
        connection.autocommit = False
        with closing(connection.cursor()) as cursor:
            try:
                if reset:
                    _truncate_operational_tables(cursor)
                else:
                    non_empty = _operational_tables_non_empty(cursor)
                    if non_empty:
                        details = ", ".join(f"{name}={count}" for name, count in sorted(non_empty.items()))
                        raise RuntimeError(f"Operational tables are not empty ({details}). Rerun with --reset.")
                _load_operational_data(cursor, cleaned_rows)
                report = _build_report(cursor, cleaning_report)
                connection.commit()
                return report
            except Exception:
                connection.rollback()
                raise


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the Online Retail dataset into PostgreSQL")
    parser.add_argument("--csv", type=Path, default=Path(__file__).resolve().parents[1] / "database_raw.csv", help="Path to database_raw.csv")
    parser.add_argument("--reset", action="store_true", help="Truncate the operational tables before loading")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        report = load_database(args.csv, reset=args.reset)
    except Exception as exc:
        print(f"LOAD FAILED: {exc}", file=sys.stderr)
        return 1

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())