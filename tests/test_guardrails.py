from pathlib import Path
from types import SimpleNamespace

from database.guardrails import GuardrailEngine

META = Path("database/schema_metadata.json")


def action(**overrides):
    base = dict(
        action_type="read",
        intent="retrieve_records",
        sql='SELECT "customers"."country" FROM "customers" WHERE "customers"."country" = %s LIMIT %s',
        params=["France", 100],
        tables=["customers"],
        columns=["country"],
        generated=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_safe_read_query_is_allowed():
    result = GuardrailEngine(META).evaluate(action())
    assert result.verdict == "allow"
    assert result.safe_to_execute


def test_write_action_is_blocked():
    result = GuardrailEngine(META).evaluate(action(action_type="write"))
    assert result.verdict == "block"
    assert not result.safe_to_execute


def test_multi_statement_is_blocked():
    result = GuardrailEngine(META).evaluate(action(sql="SELECT 1; DROP TABLE customers;"))
    assert result.verdict == "block"


def test_unknown_table_is_blocked():
    result = GuardrailEngine(META).evaluate(action(tables=["secrets"]))
    assert result.verdict == "block"
