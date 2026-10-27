from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from database.eval_week5 import evaluate_adversarial
from database.sql_generator import ActionPlan


def write_cases(path: Path, cases: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "test", "cases": cases}), encoding="utf-8")
    return path


def make_plan(sql: str) -> ActionPlan:
    return ActionPlan(
        action_type="read",
        sql=sql,
        tables=[],
        columns=[],
        explanation="Fixture.",
        confidence=1.0,
        count_sql=None,
        blocked=False,
        block_reason=None,
    )


class FixtureGenerator:
    def __init__(self, sql_by_request: dict[str, str]):
        self.metadata_path = Path("metadata.json")
        self.sql_by_request = sql_by_request

    def generate(self, request: str, session_id: str) -> ActionPlan:
        return make_plan(self.sql_by_request[request])


class FixtureGuardrails:
    def __init__(self, verdict_by_sql: dict[str, str]):
        self.verdict_by_sql = verdict_by_sql

    def evaluate(self, plan: ActionPlan, context: dict) -> SimpleNamespace:
        return SimpleNamespace(
            verdict=self.verdict_by_sql[plan.sql],
            triggered_rules=[{"rule_type": "FIXTURE"}],
        )


def test_adversarial_evaluation_checks_expectations_and_sql_constraints(tmp_path: Path) -> None:
    fixture_path = write_cases(
        tmp_path / "cases.json",
        [
            {
                "id": "safe",
                "category": "safe_query",
                "request": "safe request",
                "expected_verdict": ["ALLOW"],
            },
            {
                "id": "injection",
                "category": "injection_override",
                "request": "prompt injection",
                "expected_verdict": ["ALLOW", "BLOCK"],
                "must_not_contain_sql": ["DELETE"],
            },
            {
                "id": "write",
                "category": "ambiguous_bulk_write",
                "request": "ambiguous write",
                "expected_verdict": ["BLOCK"],
                "must_not_execute": True,
            },
        ],
    )
    sql_by_request = {
        "safe request": "SELECT country FROM customers LIMIT 10",
        "prompt injection": "DELETE FROM customers",
        "ambiguous write": "UPDATE products SET description = 'x' WHERE stock_code = 'x'",
    }
    verdict_by_sql = {
        sql_by_request["safe request"]: "ALLOW",
        sql_by_request["prompt injection"]: "BLOCK",
        sql_by_request["ambiguous write"]: "ALLOW",
    }

    results = evaluate_adversarial(
        fixture_path,
        generator_factory=lambda: FixtureGenerator(sql_by_request),
        guardrail_factory=lambda _path: FixtureGuardrails(verdict_by_sql),
    )

    assert results["total_cases"] == 3
    assert results["pass_rate_by_category"]["safe_query"]["pass_rate"] == 1.0
    assert results["unsafe_action_blocking_rate"] == 0.5
    assert results["false_positive_rate_safe_query"] == 0.0
    assert {case["id"] for case in results["failures"]} == {"injection", "write"}


def test_adversarial_pipeline_exceptions_are_recorded_as_failures(tmp_path: Path) -> None:
    fixture_path = write_cases(
        tmp_path / "cases.json",
        [
            {
                "id": "error",
                "category": "safe_query",
                "request": "request",
                "expected_verdict": ["ALLOW"],
            }
        ],
    )

    def fail_generator():
        raise RuntimeError("LLM unavailable")

    results = evaluate_adversarial(fixture_path, generator_factory=fail_generator)
    assert results["cases"][0]["verdict"] == "ERROR"
    assert results["cases"][0]["generated_sql"] == ""
    assert "LLM unavailable" in results["failures"][0]["failure_reasons"][0]


def test_adversarial_generator_blocked_for_provider_failure_is_recorded(tmp_path: Path) -> None:
    fixture_path = write_cases(
        tmp_path / "cases.json",
        [
            {
                "id": "llm-error",
                "category": "injection_override",
                "request": "request",
                "expected_verdict": ["BLOCK"],
            }
        ],
    )

    class FailedPlanGenerator(FixtureGenerator):
        def generate(self, request: str, session_id: str) -> ActionPlan:
            return ActionPlan(
                action_type="read",
                sql="",
                tables=[],
                columns=[],
                explanation="Invalid generated action plan: GROQ_API_KEY must be set.",
                confidence=0.0,
                count_sql=None,
                blocked=True,
                block_reason="Invalid generated action plan: GROQ_API_KEY must be set.",
            )

    results = evaluate_adversarial(
        fixture_path,
        generator_factory=lambda: FailedPlanGenerator(""),
    )
    assert results["cases"][0]["verdict"] == "ERROR"
    assert results["unsafe_action_blocking_rate"] == 0.0
    assert "GROQ_API_KEY" in results["failures"][0]["failure_reasons"][0]
