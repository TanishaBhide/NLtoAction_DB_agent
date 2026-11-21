"""Week 8: request-level audit trail.

The integration test runs the real agent (scripted offline planner, real
parser / validator / guardrails / executor / Week 9 modules) on requests that
never change data, then checks every audit table and removes its own session.
"""

from __future__ import annotations

import os
import uuid

import pytest

from database.audit_log import AuditLogger, parsed_intent_dict, request_trail
from database.db import DEMO_DATABASE_URL, connect
from database.intent_parser import ParsedIntent


def test_parsed_intent_dict_keeps_the_audit_fields_only():
    parsed = ParsedIntent(raw_text="x", action_type="read", intent="retrieve_records", target_tables=["customers"],
                          matched_examples=[{"big": "payload"}])
    data = parsed_intent_dict(parsed)
    assert data["action_type"] == "read" and data["target_tables"] == ["customers"]
    assert "matched_examples" not in data and "raw_text" not in data
    assert parsed_intent_dict(None) == {}


def test_audit_failure_is_a_warning_not_an_exception():
    logger = AuditLogger(database_url="postgresql://nobody@127.0.0.1:1/x")
    assert logger.record(object(), turn_no=1) is None and "audit skipped" in logger.warning


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL") or DEMO_DATABASE_URL
    try:
        connection = connect(url, connect_timeout=2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('agent_requests') IS NOT NULL AND to_regclass('drift_assessments') "
                           "IS NOT NULL")
            ready = cursor.fetchone()[0]
        connection.close()
        return url if ready else None
    except Exception:
        return None


DB_URL = _database_url()
needs_db = pytest.mark.skipif(DB_URL is None, reason="PostgreSQL with the Week 8/9 tables is not reachable")


def rows(sql, params=()):
    connection = connect(DB_URL)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else []
    finally:
        connection.close()


class Quiet:
    def step(self, _):
        pass

    def note(self, _):
        pass


@needs_db
def test_agent_writes_a_complete_audit_trail(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    from database.react_agent import build_agent

    session = f"test-audit-{uuid.uuid4().hex[:10]}"
    agent = build_agent(offline=True, session_id=session, display=Quiet(), interactive=False)
    try:
        read = agent.run("show the line items of invoice 537967")          # scripted read
        drift = agent.run("delete them")                                    # Week 9 drift -> not approved
        ddl = agent.run("truncate the products table")                      # planner block
        assert (read.status, drift.status, ddl.status) == ("success", "needs_approval", "blocked")
        assert all(run.request_id for run in (read, drift, ddl))

        stored = rows("SELECT turn_no, final_status, stop_reason, guardrail_verdict, effective_verdict, lookup_id "
                      "FROM agent_requests WHERE session_id = %s ORDER BY turn_no", (session,))
        assert [r[:5] for r in stored] == [
            (1, "success", "answered", "ALLOW", "ALLOW"),
            (2, "needs_approval", "not_approved", "ALLOW", "CONFIRM"),
            (3, "blocked", "planner_block", None, None),
        ]
        assert all(r[5] is not None for r in stored)  # schema retrieval logged for every request

        trail = request_trail(read.request_id, DB_URL)
        assert [a["execution_status"] for a in trail["attempts"]] == ["read_only"]
        assert trail["request"]["retrieved_tables"][0]["table"] in {"invoice_items", "invoices"}
        assert trail["drift"]["drift_level"] == "NO_BASELINE"

        trail = request_trail(drift.request_id, DB_URL)
        assert trail["attempts"] == []                               # never executed
        assert [w["code"] for w in trail["why_not"]] == ["INTENT_DRIFT", "APPROVAL_NOT_GIVEN"]
        assert trail["drift"]["drift_level"] == "DRIFT" and trail["drift"]["escalated"] is True
        assert trail["blast_radius"]["statement_type"] == "DELETE"
        assert trail["request"]["escalations"][0]["source"] == "drift"

        assert [w["code"] for w in request_trail(ddl.request_id, DB_URL)["why_not"]] == ["PLANNER_DDL"]
        view = rows("SELECT request_id, root_cause_code, sandbox_runs FROM v_request_audit WHERE session_id = %s "
                    "ORDER BY request_id", (session,))
        assert [(v[1], v[2]) for v in view] == [(None, 1), ("INTENT_DRIFT", 0), ("PLANNER_DDL", 0)]
    finally:
        rows("DELETE FROM schema_lookup_log WHERE session_id = %s", (session,))
        rows("DELETE FROM agent_sessions WHERE session_id = %s", (session,))
