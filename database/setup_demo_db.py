"""Create (or reset) the demo database for Weeks 6-7 without needing psql.

Applies, in order: the operational schema, the sample rows, the agent schema,
and the Week 5 and Week 6/7 migrations. The operational and agent schema files
drop and recreate their tables, so this is a full reset.

    python database/setup_demo_db.py
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
SQL_FILES = [
    "schema.sql",
    "sample_data.sql",
    "agent_schema.sql",
    "agent_schema_week5.sql",
    "agent_schema_week6_7.sql",
]


def apply_all(database_url: str | None = None, *, quiet: bool = False) -> None:
    connection = connect(database_url)
    try:
        connection.autocommit = False
        with connection.cursor() as cursor:
            for name in SQL_FILES:
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
    args = parser.parse_args(argv)
    url = args.database_url or os.environ.get("DATABASE_URL") or (None if has_database_config() else DEMO_DATABASE_URL)
    shown = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", url) if url else "from PG* environment"
    print(f"Resetting demo database ({shown})")
    apply_all(url)
    print("Done: operational tables, 13 sample line items, agent + Week 5/6/7 tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
