from __future__ import annotations

import json
from pathlib import Path

import pytest

from database.llm_client import GroqClient, MockLLMClient, create_llm_client
from database.sql_generator import SQLGenerator
from database.eval_week4 import evaluate


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests" / "representative_queries.json").read_text(encoding="utf-8"))


def response(sql: str, tables: list[str], columns: list[str], action_type: str = "read", count_sql: str | None = None) -> dict:
    return {
        "action_type": action_type,
        "sql": sql,
        "tables": tables,
        "columns": columns,
        "explanation": "Fixture response.",
        "confidence": 0.9,
        "count_sql": count_sql,
        "blocked": False,
        "block_reason": None,
    }


def test_read_plan_is_limited_and_valid() -> None:
    client = MockLLMClient(
        response(
            "SELECT customer_id, country FROM customers WHERE country = 'France' LIMIT 100",
            ["customers"],
            ["customer_id", "country"],
        )
    )
    plan = SQLGenerator(llm_client=client).generate("show customers from France")

    assert not plan.blocked
    assert plan.action_type == "read"
    assert "LIMIT" in plan.sql.upper()
    assert plan.count_sql is None
    assert len(client.calls) == 1


def test_update_plan_has_scoped_count_sql() -> None:
    client = MockLLMClient(
        response(
            "UPDATE invoice_items SET unit_price = 5.00 WHERE stock_code = '85123A'",
            ["invoice_items"],
            ["unit_price", "stock_code"],
            action_type="write",
            count_sql="SELECT COUNT(*) FROM invoice_items WHERE stock_code = '85123A'",
        )
    )
    plan = SQLGenerator(llm_client=client).generate(
        "set the price of product with stock code 85123A to 5.00"
    )

    assert plan.action_type == "write"
    assert not plan.blocked
    assert "WHERE" in plan.sql.upper()
    assert plan.count_sql == "SELECT COUNT(*) FROM invoice_items WHERE stock_code = '85123A'"


def test_representative_queries_match_expected_action_types_offline() -> None:
    assert len(CASES) == 22
    assert sum(case["expected_action_type"] == "read" for case in CASES) == 12
    assert sum(
        case["expected_action_type"] == "write" and not case.get("expected_blocked", False)
        for case in CASES
    ) == 6
    assert sum(case["expected_action_type"] == "ddl" for case in CASES) == 3
    assert sum(case.get("expected_blocked", False) for case in CASES) == 4
    responses = [case["mock_response"] for case in CASES if case["mock_response"] is not None]
    client = MockLLMClient(responses)
    generator = SQLGenerator(llm_client=client)

    for index, case in enumerate(CASES):
        plan = generator.generate(case["query"], session_id=f"fixture-{index}")
        assert plan.action_type == case["expected_action_type"], case["query"]
        assert plan.blocked is case.get("expected_blocked", False), case["query"]

    assert len(client.calls) == sum(case["mock_response"] is not None for case in CASES)


def test_generator_uses_sql_identifiers_not_llm_reported_columns() -> None:
    case = next(case for case in CASES if case["query"] == "how many invoices per country")
    plan = SQLGenerator(llm_client=MockLLMClient(case["mock_response"])).generate(case["query"])

    assert not plan.blocked
    assert "invoice_count" not in plan.columns
    assert set(plan.columns) == {"country", "invoice_no", "customer_id"}


def test_prompt_contains_schema_columns_keys_joins_and_matching_example() -> None:
    generator = SQLGenerator(llm_client=MockLLMClient([]))
    parsed = generator.intent_parser.parse("how many invoices per country")
    _, user_prompt, _ = generator.prompt_builder.build(parsed, parsed.raw_text)
    prompt_data = json.loads(user_prompt)
    schema_by_table = {table["name"]: table for table in prompt_data["schema"]}

    assert {column["name"] for column in schema_by_table["customers"]["columns"]} == {
        "customer_id",
        "country",
    }
    assert "PRIMARY KEY" in schema_by_table["customers"]["columns"][0]["markers"]
    assert "invoices.customer_id -> customers.customer_id" in prompt_data["primary_join_paths"]
    assert any(
        example["question"] == "how many invoices per country"
        for example in prompt_data["few_shot_examples"]
    )


def test_one_validation_retry_can_correct_invalid_identifier() -> None:
    bad_response = response(
        "SELECT c.country_name, COUNT(i.invoice_no) AS invoice_count "
        "FROM customers AS c JOIN invoices AS i ON i.customer_id = c.customer_id "
        "GROUP BY c.country_name LIMIT 100",
        ["customers", "invoices"],
        ["country_name", "invoice_no", "customer_id", "invoice_count"],
    )
    good_response = response(
        "SELECT c.country, COUNT(i.invoice_no) AS invoice_count "
        "FROM customers AS c JOIN invoices AS i ON i.customer_id = c.customer_id "
        "GROUP BY c.country LIMIT 100",
        ["customers", "invoices"],
        ["country", "invoice_no", "customer_id", "invoice_count"],
    )
    client = MockLLMClient([bad_response, good_response])
    plan = SQLGenerator(llm_client=client).generate("how many invoices per country")

    assert not plan.blocked
    assert "country_name" not in plan.sql
    assert len(client.calls) == 2
    retry_prompt = str(client.calls[1]["user"])
    assert "country_name" in retry_prompt
    assert '"customers": ["country", "customer_id"]' in retry_prompt


def test_blocked_after_one_failed_retry_names_identifier_and_zeroes_confidence() -> None:
    bad_response = response(
        "SELECT c.country_name FROM customers AS c LIMIT 100",
        ["customers"],
        ["country_name"],
    )
    client = MockLLMClient([bad_response, bad_response])
    plan = SQLGenerator(llm_client=client).generate("show customers from France")

    assert plan.blocked
    assert plan.confidence == 0.0
    assert "country_name" in (plan.block_reason or "")
    assert len(client.calls) == 2


def test_ddl_is_blocked_without_calling_llm() -> None:
    client = MockLLMClient([])
    plan = SQLGenerator(llm_client=client).generate("drop the invoices table")

    assert plan.action_type == "ddl"
    assert plan.blocked
    assert plan.sql == ""
    assert plan.block_reason
    assert client.calls == []


def test_unscoped_delete_is_blocked_without_calling_llm() -> None:
    client = MockLLMClient([])
    plan = SQLGenerator(llm_client=client).generate("delete everything")

    assert plan.action_type == "write"
    assert plan.blocked
    assert client.calls == []


def test_malformed_llm_output_returns_blocked_plan() -> None:
    plan = SQLGenerator(llm_client=MockLLMClient([{"action_type": "read"}, {"action_type": "read"}])).generate(
        "show customers from France"
    )

    assert plan.blocked
    assert plan.sql == ""
    assert "missing required fields" in (plan.block_reason or "")


def test_write_without_where_or_matching_count_is_blocked() -> None:
    invalid_response = response(
        "UPDATE customers SET country = 'France'",
        ["customers"],
        ["country"],
        action_type="write",
        count_sql="SELECT COUNT(*) FROM customers",
    )
    client = MockLLMClient([invalid_response, invalid_response])
    plan = SQLGenerator(llm_client=client).generate(
        "set the country of customer 17850 to France"
    )

    assert plan.blocked
    assert plan.block_reason


def test_groq_is_default_provider_and_uses_gpt_oss_20b(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    client = create_llm_client()

    assert isinstance(client, GroqClient)
    assert client._model == "openai/gpt-oss-20b"
    assert str(client._client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"


def test_groq_requires_groq_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        create_llm_client()


def test_groq_model_can_be_overridden_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL", "custom/groq-model")

    client = create_llm_client("groq")

    assert client._model == "custom/groq-model"


def test_required_regression_requests_are_present() -> None:
    queries = {case["query"] for case in CASES}
    assert {
        "how many invoices per country",
        "revenue per country",
        "top 5 customers by number of invoices",
        "show customers from France",
    }.issubset(queries)


def test_evaluation_shows_reduced_identifier_rejection_rate() -> None:
    before, after, cases, *_ = evaluate()

    assert (before, cases) == (15, 18)
    assert (after, cases) == (18, 18)
