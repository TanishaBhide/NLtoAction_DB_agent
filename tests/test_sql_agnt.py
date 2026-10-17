import json
from pathlib import Path
from types import SimpleNamespace

from database.sql_agent import SQLAgent

META = Path("database/schema_metadata.json")


def intent(**overrides):
    base = dict(
        raw_text="show customers from France",
        action_type="read",
        intent="retrieve_records",
        target_tables=["customers"],
        target_columns=["customer_id", "country"],
        constraints=[{"table": "customers", "column": "country", "operator": "=", "value": "France"}],
        set_values={}, aggregation=None, order_by=[], limit=None, confidence=0.9,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_read_query_is_parameterized_and_limited():
    action = SQLAgent(META).generate(intent())
    assert action.generated
    assert action.sql.startswith("SELECT ")
    assert '"customers"."country" = %s' in action.sql
    assert "LIMIT %s" in action.sql
    assert "France" not in action.sql
    assert "France" in action.params


def test_unscoped_update_is_not_generated():
    action = SQLAgent(META).generate(intent(action_type="write", intent="update_record", set_values={"country": "Italy"}, constraints=[]))
    assert not action.generated
    assert action.sql is None
    assert action.requires_approval


def test_ddl_is_not_generated():
    action = SQLAgent(META).generate(intent(action_type="ddl", intent="drop_table", target_tables=["customers"]))
    assert not action.generated
    assert action.sql is None
