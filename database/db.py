"""Shared PostgreSQL connection settings for the Week 6/7 execution stack."""

from __future__ import annotations

import os
from typing import Any

DEMO_DATABASE_URL = "postgresql://agent:agent@localhost:55432/online_retail_db"


def connection_options() -> tuple[str | None, dict[str, str]]:
    """Return (DATABASE_URL, libpq keyword options) from the environment."""
    database_url = os.environ.get("DATABASE_URL")
    options = {
        key: os.environ[variable]
        for variable, key in (
            ("PGHOST", "host"),
            ("PGPORT", "port"),
            ("PGDATABASE", "dbname"),
            ("PGUSER", "user"),
            ("PGPASSWORD", "password"),
        )
        if os.environ.get(variable)
    }
    return database_url, options


def has_database_config() -> bool:
    database_url, options = connection_options()
    return bool(database_url or options.get("dbname") or options.get("host"))


def connect(database_url: str | None = None, *, connect_timeout: int = 5) -> Any:
    """Open a new psycopg2 connection (explicit URL first, then the environment)."""
    import psycopg2

    env_url, options = connection_options()
    url = database_url or env_url
    if url:
        return psycopg2.connect(url, connect_timeout=connect_timeout)
    if not options:
        raise RuntimeError(
            "No PostgreSQL connection configured. Set DATABASE_URL "
            f"(for the Docker demo database: {DEMO_DATABASE_URL})."
        )
    return psycopg2.connect(connect_timeout=connect_timeout, **options)
