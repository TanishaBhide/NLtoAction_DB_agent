"""Create (or reset) the demo database for Weeks 6-9 without needing psql.

Applies, in order: the operational schema, the data, the agent schema, the
Week 5, 6/7, 8 and 9 migrations and the web app's role-based views. The
operational and agent schema files drop and recreate their tables, so this is
a full reset.

Datasets:
  large   (default) the 13 real sample rows + sample_data_large.sql, a seeded
          Online Retail-style dataset with 2,000 line items in total
          (regenerate with database/generate_retail_data.py)
  sample  only the 13 sample rows; the Week 6-9 demo scenarios and the Week 9
          evaluation are written for this one and reset to it themselves

    python database/setup_demo_db.py
    python database/setup_demo_db.py --dataset sample
    python database/setup_demo_db.py --database-url postgresql://user:pw@host:5432/db
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

try:
    from .db import DEMO_DATABASE_URL, connect, has_database_config
except ImportError:
    from db import DEMO_DATABASE_URL, connect, has_database_config

HERE = Path(__file__).parent
DATASETS = {
    "sample": ["sample_data.sql"],
    "large": ["sample_data.sql", "sample_data_large.sql"],
}
SQL_FILES = [
    "schema.sql",
    "<data>",
    "agent_schema.sql",
    "agent_schema_week5.sql",
    "agent_schema_week6_7.sql",
    "agent_schema_week8.sql",
    "agent_schema_week9.sql",
    "agent_schema_web.sql",
]


def apply_all(database_url: str | None = None, *, quiet: bool = False, dataset: str = "large") -> None:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; choose one of {', '.join(DATASETS)}")
    files = [f for name in SQL_FILES for f in (DATASETS[dataset] if name == "<data>" else [name])]
    connection = connect(database_url)
    try:
        connection.autocommit = False
        with connection.cursor() as cursor:
            for name in files:
                cursor.execute((HERE / name).read_text(encoding="utf-8"))
                if not quiet:
                    print(f"  applied {name}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dataset", choices=sorted(DATASETS), default="large",
                        help="large = 2,000 line items (default); sample = the 13 original rows")
    args = parser.parse_args(argv)
    url = args.database_url or os.environ.get("DATABASE_URL") or (None if has_database_config() else DEMO_DATABASE_URL)
    shown = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", url) if url else "from PG* environment"
    print(f"Resetting demo database ({shown}) with the {args.dataset} dataset")
    apply_all(url, dataset=args.dataset)
    connection = connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM products), "
                           "(SELECT COUNT(*) FROM invoices), (SELECT COUNT(*) FROM invoice_items)")
            c, p, i, ii = cursor.fetchone()
    finally:
        connection.close()
    print(f"Done: {c} customers, {p} products, {i} invoices, {ii} line items; agent + Week 5-9 tables and views.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
