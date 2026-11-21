"""Minimal .env loader (no python-dotenv dependency).

Reads KEY=VALUE lines from the project's .env file into os.environ. Variables
that are already set in the environment win. Accepts spaces around '=',
optional quotes, 'export ' prefixes and '#' comments. Values are never
printed. Called by entry points (web server, CLIs), not at import time, so
tests never pick up a live API key by accident.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | str | None = None) -> list[str]:
    """Load the .env file; return the NAMES of variables that were set (never the values)."""
    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.is_file():
        return []
    loaded = []
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
