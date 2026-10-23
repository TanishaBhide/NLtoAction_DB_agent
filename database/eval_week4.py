"""Offline, fixture-based evaluation of the Week 4 SQL generation pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .llm_client import MockLLMClient
    from .sql_generator import SQLGenerator
except ImportError:
    from llm_client import MockLLMClient
    from sql_generator import SQLGenerator


CASES_PATH = Path(__file__).parents[1] / "tests" / "representative_queries.json"


def evaluate(cases_path: Path = CASES_PATH) -> tuple[int, int, int, int, int, int, int]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    fixture_responses = [case["mock_response"] for case in cases if case["mock_response"] is not None]
    generator = SQLGenerator(llm_client=MockLLMClient(fixture_responses))

    valid_sql = 0
    legacy_valid_sql = 0
    sql_cases = 0
    correct_actions = 0
    ddl_blocked = 0
    ddl_cases = 0
    for index, case in enumerate(cases):
        plan = generator.generate(case["query"], session_id=f"week4-eval-{index}")
        correct_actions += plan.action_type == case["expected_action_type"]
        expected_blocked = case.get("expected_blocked", False)
        if not expected_blocked:
            sql_cases += 1
            response = case["mock_response"]
            valid = not plan.blocked and bool(plan.sql)
            if valid:
                validation = generator.validator.validate(plan.sql, plan.action_type)
                valid = validation.valid
            if valid and plan.action_type == "write":
                valid = bool(plan.count_sql) and generator.validator.validate_count_sql(
                    plan.count_sql or "",
                    plan.sql,
                ).valid
            valid_sql += valid

            # Reproduce Week 4's former trust in the LLM's self-reported identifiers.
            legacy_valid = not plan.blocked and bool(plan.sql)
            if legacy_valid:
                legacy_valid = validation.valid
            known_tables = set(generator.validator.tables)
            known_columns = {
                column for columns in generator.validator.tables.values() for column in columns
            }
            if legacy_valid:
                reported_tables = {name.casefold() for name in response.get("tables", [])}
                reported_columns = {name.casefold() for name in response.get("columns", [])}
                legacy_valid = (
                    all(name.casefold() in known_tables for name in response.get("tables", []))
                    and all(name.casefold() in known_columns for name in response.get("columns", []))
                    and set(name.casefold() for name in validation.tables).issubset(reported_tables)
                    and set(name.casefold() for name in validation.columns).issubset(reported_columns)
                )
            if legacy_valid and plan.action_type == "write":
                legacy_valid = bool(plan.count_sql) and generator.validator.validate_count_sql(
                    plan.count_sql or "",
                    plan.sql,
                ).valid
            legacy_valid_sql += legacy_valid
        if case["expected_action_type"] == "ddl":
            ddl_cases += 1
            ddl_blocked += plan.blocked and plan.action_type == "ddl"

    return (
        legacy_valid_sql,
        valid_sql,
        sql_cases,
        correct_actions,
        len(cases),
        ddl_blocked,
        ddl_cases,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Week 4 using offline representative-query fixtures.")
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="Representative query JSON file")
    args = parser.parse_args()
    legacy_valid, valid_sql, sql_cases, correct_actions, total, ddl_blocked, ddl_cases = evaluate(args.cases)
    print("Week 4 offline fixture evaluation")
    print(
        "Valid-SQL rate before fix (legacy LLM identifier checks): "
        f"{legacy_valid}/{sql_cases} ({legacy_valid / sql_cases:.1%})"
    )
    print(f"Valid-SQL rate after fix: {valid_sql}/{sql_cases} ({valid_sql / sql_cases:.1%})")
    print(f"Correct-action-type rate: {correct_actions}/{total} ({correct_actions / total:.1%})")
    print(f"DDL-blocked rate: {ddl_blocked}/{ddl_cases} ({ddl_blocked / ddl_cases:.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
