"""Held-out evaluation for the intent parser.

Two sets of sentences that were NOT used as unit tests:

- heldout_cases.json (round 1): written before the Week 3 parser fixes.
  Baseline before the fixes: 6/15. The fixes were designed after seeing these
  failures, so the round-1 score after the fixes overstates generalisation.
- heldout_cases_round2.json (round 2): written after the fixes and run once
  without tuning. First run: 14/15 (U13 'only cancelled ones' follow-up missed
  the status filter). U13 was fixed afterwards and is reported as such.

This test prints the pass rates and never fails the build: it measures
generalisation, it is not a regression gate. Run with:
    pytest tests/test_heldout.py -s
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / "database"
if str(DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(DATABASE_DIR))

from intent_parser import IntentParser

HERE = Path(__file__).parent
CASE_FILES = [HERE / "heldout_cases.json", HERE / "heldout_cases_round2.json"]


def _check(parsed, expect: dict) -> list[str]:
    problems: list[str] = []
    if "action_type" in expect and parsed.action_type not in expect["action_type"]:
        problems.append(f"action_type={parsed.action_type}, expected one of {expect['action_type']}")
    if "intent" in expect and parsed.intent not in expect["intent"]:
        problems.append(f"intent={parsed.intent}, expected one of {expect['intent']}")
    if "is_destructive" in expect and parsed.is_destructive != expect["is_destructive"]:
        problems.append(f"is_destructive={parsed.is_destructive}, expected {expect['is_destructive']}")
    for wanted in expect.get("constraints", []):
        found = any(
            c.get("column") == wanted["column"]
            and c.get("operator") == wanted["operator"]
            and str(c.get("value")).lower() == str(wanted["value"]).lower()
            for c in parsed.constraints
        )
        if not found:
            problems.append(f"missing constraint {wanted}")
    for column, value in expect.get("set_values", {}).items():
        actual = (parsed.set_values or {}).get(column)
        if str(actual).lower() != str(value).lower():
            problems.append(f"set_values[{column}]={actual!r}, expected {value!r}")
    return problems


def run_heldout(cases_path: Path) -> tuple[int, int, list[str]]:
    parser = IntentParser.from_paths()
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    passed = 0
    report: list[str] = []
    for case in cases:
        session_id = f"heldout-{case['id']}"
        parsed = None
        for turn in case["turns"]:
            parsed = parser.parse(turn, session_id=session_id)
        problems = _check(parsed, case["expect"])
        if problems:
            report.append(f"FAIL {case['id']} {case['turns'][-1]!r}: " + "; ".join(problems))
        else:
            passed += 1
            report.append(f"PASS {case['id']} {case['turns'][-1]!r}")
    return passed, len(cases), report


def test_heldout_pass_rate() -> None:
    print()
    for cases_path in CASE_FILES:
        passed, total, report = run_heldout(cases_path)
        print(f"--- {cases_path.name}")
        print("\n".join(report))
        print(f"HELD-OUT PASS RATE ({cases_path.name}): {passed}/{total} = {passed / total:.0%}")
        assert total == 15


if __name__ == "__main__":
    for path in CASE_FILES:
        p, t, r = run_heldout(path)
        print(f"--- {path.name}")
        print("\n".join(r))
        print(f"HELD-OUT PASS RATE ({path.name}): {p}/{t} = {p / t:.0%}")