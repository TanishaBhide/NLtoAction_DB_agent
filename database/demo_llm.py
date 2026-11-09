"""Offline, deterministic stand-in for the LLM (for demos and tests without an API key).

It answers only the requests listed in demo_scenarios.json. Each scenario lists
one plan per attempt; the agent's feedback text says how many attempts were
already made ("Previous attempts: N"), and the client returns plan N. Scenarios
deliberately include realistic first-attempt mistakes (an unquoted VARCHAR
identifier, a case-sensitive LIKE) so the ReAct retry path can be shown
without a live model. Everything downstream (parser, validator, guardrails,
executor, PostgreSQL) is real.

Unknown requests get a blocked plan explaining that a live LLM key is needed.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_SCENARIOS = Path(__file__).with_name("demo_scenarios.json")
ATTEMPT_RE = re.compile(r"Previous attempts: (\d+)")


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def load_scenarios(path: Path | str | None = None) -> list[dict[str, Any]]:
    return json.loads(Path(path or DEFAULT_SCENARIOS).read_text(encoding="utf-8"))["scenarios"]


class ScriptedLLMClient:
    def __init__(self, scenarios_path: Path | str | None = None) -> None:
        self.scripts: dict[str, list[dict[str, Any]]] = {}
        for scenario in load_scenarios(scenarios_path):
            for turn in scenario.get("turns", [scenario]):
                if turn.get("attempts"):
                    self.scripts[normalize(turn["request"])] = turn["attempts"]
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, user: str, tool_schema: dict, *, context: str | None = None) -> dict:
        payload, _ = json.JSONDecoder().raw_decode(user)
        request = str(payload.get("request", ""))
        action_type = str(payload.get("parsed_intent", {}).get("action_type", "read"))
        found = ATTEMPT_RE.findall(user)
        index = int(found[-1]) if found else 0
        self.calls.append({"request": request, "attempt_index": index, "user_prompt": user})
        attempts = self.scripts.get(normalize(request))
        if not attempts:
            return {
                "action_type": action_type if action_type in {"read", "write"} else "read",
                "sql": "",
                "tables": [],
                "columns": [],
                "explanation": "Offline demo mode.",
                "confidence": 0.0,
                "count_sql": None,
                "blocked": True,
                "block_reason": (
                    "Offline demo mode only knows the scripted demo requests "
                    "(see database/demo_scenarios.json). Set GROQ_API_KEY to ask anything."
                ),
            }
        plan = copy.deepcopy(attempts[min(index, len(attempts) - 1)])
        plan.setdefault("action_type", action_type)
        plan.setdefault("tables", [])
        plan.setdefault("columns", [])
        plan.setdefault("explanation", "Scripted demo plan.")
        plan.setdefault("confidence", 0.9)
        plan.setdefault("count_sql", None)
        plan.setdefault("blocked", False)
        plan.setdefault("block_reason", None)
        return plan
