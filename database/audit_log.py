"""Week 8: request-level audit persistence and the audit-trail CLI.

For every natural-language request the agent finishes, one transaction writes:

    schema_lookup_log      the schema retrieval that grounded the prompt (top-k tables + scores)
    agent_requests         parsed intent, final SQL, Week 5 verdict, effective verdict after the
                           Week 9 escalations, outcome, stop reason and the plain-English response
    request_attempts       links to every execution_audit row (one per sandboxed attempt)
    request_explanations   the ordered "why not" root causes
    drift_assessments      Week 9 intent-drift score and features (if computed)
    blast_radius_previews  Week 9 preview plus whether execution confirmed it (if computed)

The audit is written on its own connection, after the run, and a failure only
produces a warning: auditing must never change what the agent did.

    python database/audit_log.py --recent 10
    python database/audit_log.py --session demo-1234
    python database/audit_log.py --request 7          # full trail of one request
    python database/audit_log.py --why 7              # only its root causes
    python database/audit_log.py --stats              # outcome / root-cause / novelty summary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any

try:
    from .db import DEMO_DATABASE_URL, connect, has_database_config
except ImportError:
    from db import DEMO_DATABASE_URL, connect, has_database_config

PARSED_FIELDS = (
    "action_type", "intent", "target_tables", "target_columns", "constraints", "set_values", "aggregation",
    "order_by", "limit", "confidence", "ambiguities", "is_destructive", "is_unscoped", "resolved_from_context",
    "previous_request",
)


def parsed_intent_dict(parsed: Any) -> dict[str, Any]:
    if parsed is None:
        return {}
    data = asdict(parsed) if hasattr(parsed, "__dataclass_fields__") else dict(parsed)
    return {key: data.get(key) for key in PARSED_FIELDS if key in data}


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


class AuditLogger:
    def __init__(self, *, database_url: str | None = None, enabled: bool = True) -> None:
        self.database_url = database_url
        self.enabled = enabled
        self.warning: str | None = None

    def record(self, run: Any, *, turn_no: int) -> int | None:
        """Persist one finished AgentRun; returns agent_requests.request_id (or None)."""
        if not self.enabled:
            return None
        try:
            connection = connect(self.database_url)
        except Exception as error:  # noqa: BLE001
            self.warning = f"audit skipped: {error}"
            return None
        try:
            with connection, connection.cursor() as cursor:
                request_id = self._write(cursor, run, turn_no)
            self.warning = None
            return request_id
        except Exception as error:  # noqa: BLE001 - auditing never masks the run
            self.warning = f"audit write failed: {type(error).__name__}: {str(error).strip()}"
            return None
        finally:
            connection.close()

    def _write(self, cursor: Any, run: Any, turn_no: int) -> int:
        session_id = run.session_id[:36]
        cursor.execute(
            "INSERT INTO agent_sessions (session_id, purpose) VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING",
            (session_id, "NL-to-Action agent session"),
        )
        hits = getattr(run, "schema_hits", []) or []
        parsed = getattr(run, "parsed_intent", {}) or {}
        cursor.execute(
            "INSERT INTO schema_lookup_log (session_id, user_query, retrieved_tables, retrieved_columns, "
            "retrieval_score) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s) RETURNING lookup_id",
            (
                session_id,
                run.request,
                _json(hits),
                _json(parsed.get("target_columns") or []),
                min(max(hits[0]["score"], 0), 9.9999) if hits else None,
            ),
        )
        lookup_id = cursor.fetchone()[0]

        plan, guardrail, execution, response = run.plan, run.guardrail, run.execution, run.response
        cursor.execute(
            """
            INSERT INTO agent_requests (
                session_id, turn_no, request_text, parsed_intent, lookup_id, action_type, final_sql, count_sql,
                plan_confidence, guardrail_verdict, effective_verdict, risk_score, triggered_rules, escalations,
                approved, final_status, stop_reason, attempts, row_count, affected_rows, response_headline,
                response_text, latency_ms
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING request_id
            """,
            (
                session_id,
                turn_no,
                run.request,
                _json(parsed),
                lookup_id,
                plan.action_type if plan else parsed.get("action_type", "unknown"),
                (plan.sql or None) if plan else None,
                plan.count_sql if plan else None,
                round(float(plan.confidence), 3) if plan else None,
                guardrail.verdict if guardrail else None,
                getattr(run, "effective_verdict", None) or (guardrail.verdict if guardrail else None),
                guardrail.risk_score if guardrail else None,
                _json(guardrail.triggered_rules if guardrail else []),
                _json(getattr(run, "escalations", []) or []),
                bool(getattr(run, "approved", False)),
                run.status,
                getattr(run, "stop_reason", "") or "unknown",
                run.attempts,
                execution.row_count if execution and execution.action_type == "read" else None,
                execution.affected_rows if execution else None,
                response.headline if response else run.answer,
                response.text() if response else run.answer,
                getattr(run, "latency_ms", None),
            ),
        )
        request_id = int(cursor.fetchone()[0])

        for attempt_no, audit_id in getattr(run, "audit_ids", []) or []:
            cursor.execute(
                "INSERT INTO request_attempts (request_id, audit_id, attempt_no) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (request_id, audit_id, attempt_no),
            )
        for seq, item in enumerate(response.why_not if response else [], 1):
            cursor.execute(
                "INSERT INTO request_explanations (request_id, explanation_seq, stage, code, headline, cause, fix, "
                "evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (request_id, seq, item.stage, item.code[:60], item.headline, item.cause, item.fix,
                 _json(item.evidence)),
            )

        drift = getattr(run, "drift", None)
        if drift is not None:
            cursor.execute(
                "INSERT INTO drift_assessments (request_id, drift_score, drift_level, baseline_turns, escalated, "
                "features, reasons) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
                (request_id, drift.score, drift.level, drift.baseline_turns, drift.escalate,
                 _json(drift.features), _json(drift.reasons)),
            )
        blast = getattr(run, "blast_radius", None)
        if blast is not None and blast.target_table != "?":
            cursor.execute(
                """
                INSERT INTO blast_radius_previews (
                    request_id, target_table, statement_type, direct_rows, cascaded_rows, tables_touched,
                    radius_level, predicted_failure, impacts, triggers, spread, preview_ms,
                    actual_affected_rows, actual_sqlstate, prediction_correct
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                """,
                (request_id, blast.target_table, blast.statement_type, blast.direct_rows, blast.cascaded_rows,
                 blast.tables_touched, blast.level,
                 (f"{blast.predicted_sqlstate}: {blast.predicted_failure}" if blast.predicted_failure else
                  (f"preview error: {blast.error}" if blast.error else None)),
                 _json([asdict(i) for i in blast.impacts]), _json(blast.triggers), _json(blast.spread),
                 blast.duration_ms, blast.actual_affected_rows, blast.actual_sqlstate, blast.prediction_correct),
            )
        return request_id


# ---------------------------------------------------------------- queries
def fetch(sql: str, params: tuple = (), database_url: str | None = None) -> tuple[list[str], list[list[Any]]]:
    connection = connect(database_url)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [d[0] for d in cursor.description], [list(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def request_trail(request_id: int, database_url: str | None = None) -> dict[str, Any]:
    """Everything recorded about one request, from intent to response."""
    columns, rows = fetch(
        "SELECT r.*, l.retrieved_tables, l.retrieved_columns, l.retrieval_score "
        "FROM agent_requests r LEFT JOIN schema_lookup_log l ON l.lookup_id = r.lookup_id WHERE r.request_id = %s",
        (request_id,), database_url,
    )
    if not rows:
        raise LookupError(f"request {request_id} not found")
    trail: dict[str, Any] = {"request": dict(zip(columns, rows[0]))}
    columns, rows = fetch(
        "SELECT ra.attempt_no, ea.audit_id, ea.execution_status, ea.guardrail_verdict, ea.sqlstate, "
        "ea.error_category, ea.affected_row_count, ea.transaction_id, ea.duration_ms, ea.generated_sql, "
        "ea.error_message FROM request_attempts ra JOIN execution_audit ea ON ea.audit_id = ra.audit_id "
        "WHERE ra.request_id = %s ORDER BY ra.attempt_no, ea.audit_id",
        (request_id,), database_url,
    )
    trail["attempts"] = [dict(zip(columns, row)) for row in rows]
    columns, rows = fetch(
        "SELECT explanation_seq, stage, code, headline, cause, fix, evidence FROM request_explanations "
        "WHERE request_id = %s ORDER BY explanation_seq", (request_id,), database_url,
    )
    trail["why_not"] = [dict(zip(columns, row)) for row in rows]
    for key, table in (("drift", "drift_assessments"), ("blast_radius", "blast_radius_previews")):
        columns, rows = fetch(f"SELECT * FROM {table} WHERE request_id = %s", (request_id,), database_url)
        trail[key] = dict(zip(columns, rows[0])) if rows else None
    return trail


def print_trail(trail: dict[str, Any], display: Any) -> None:
    r = trail["request"]
    display.rule(f"request #{r['request_id']} · session {r['session_id']} · turn {r['turn_no']}", "cyan")
    display.out(f"  request   : {r['request_text']}")
    parsed = r["parsed_intent"] or {}
    display.out(f"  intent    : {parsed.get('action_type')} / {parsed.get('intent')} · tables "
                f"{parsed.get('target_tables')} · constraints {parsed.get('constraints')}"
                + (" · follow-up" if parsed.get("resolved_from_context") else ""))
    hits = ", ".join(f"{h['table']} ({h['score']:.2f})" for h in (r["retrieved_tables"] or []))
    display.out(f"  retrieval : {hits or '-'}")
    display.out(f"  SQL       : {r['final_sql'] or '-'}")
    display.out(f"  verdict   : guardrails {r['guardrail_verdict']} (risk {r['risk_score']}) -> effective "
                f"{r['effective_verdict']}" + (" · approved" if r["approved"] else ""))
    if r["escalations"]:
        for item in r["escalations"]:
            display.out(f"              escalated by {item['source']}: {item['reason']}")
    if trail["drift"]:
        d = trail["drift"]
        display.out(f"  drift     : {d['drift_level']} score {d['drift_score']} over {d['baseline_turns']} turn(s)")
    if trail["blast_radius"]:
        b = trail["blast_radius"]
        display.out(f"  blast     : {b['radius_level']} · direct {b['direct_rows']} · cascaded {b['cascaded_rows']} · "
                    f"predicted failure {b['predicted_failure'] or 'none'} · prediction correct "
                    f"{b['prediction_correct']}")
    if trail["attempts"]:
        display.out("  attempts  :")
        display.table(["try", "audit", "status", "sqlstate", "rows", "txid", "ms"],
                      [[a["attempt_no"], a["audit_id"], a["execution_status"], a["sqlstate"] or "",
                        a["affected_row_count"], a["transaction_id"] or "", a["duration_ms"]]
                       for a in trail["attempts"]], indent="    ")
    display.out(f"  outcome   : {r['final_status']} ({r['stop_reason']}) after {r['attempts']} attempt(s), "
                f"{r['latency_ms']} ms")
    display.out("  response  :")
    for line in (r["response_text"] or "").splitlines():
        display.out(f"    {line}")


STATS_QUERIES = {
    "Outcomes": "SELECT final_status, stop_reason, COUNT(*) AS requests FROM agent_requests "
                "GROUP BY 1, 2 ORDER BY 3 DESC, 1",
    "Root causes (first why-not of each request)":
        "SELECT stage, code, COUNT(*) AS requests FROM request_explanations WHERE explanation_seq = 1 "
        "GROUP BY 1, 2 ORDER BY 3 DESC, 2",
    "Week 9: escalations by novelty module":
        "SELECT e ->> 'source' AS module, e ->> 'code' AS code, COUNT(*) AS requests FROM agent_requests, "
        "jsonb_array_elements(escalations) AS e GROUP BY 1, 2 ORDER BY 3 DESC",
    "Week 9: intent drift levels":
        "SELECT drift_level, COUNT(*) AS requests, ROUND(AVG(drift_score), 3) AS avg_score, "
        "SUM(CASE WHEN escalated THEN 1 ELSE 0 END) AS escalated FROM drift_assessments GROUP BY 1 ORDER BY 1",
    "Week 9: blast-radius previews":
        "SELECT radius_level, COUNT(*) AS previews, SUM(direct_rows) AS direct_rows, "
        "SUM(cascaded_rows) AS cascaded_rows, SUM(CASE WHEN predicted_failure IS NOT NULL THEN 1 ELSE 0 END) "
        "AS predicted_failures, SUM(CASE WHEN prediction_correct THEN 1 ELSE 0 END) AS correct, "
        "SUM(CASE WHEN prediction_correct IS NOT NULL THEN 1 ELSE 0 END) AS verified "
        "FROM blast_radius_previews GROUP BY 1 ORDER BY 1",
}


def main(argv: list[str] | None = None) -> int:
    try:
        from .display import Display
    except ImportError:
        from display import Display

    parser = argparse.ArgumentParser(description="Query the Week 8 audit trail.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--recent", type=int, metavar="N", help="Latest N requests (default 15)")
    group.add_argument("--session", help="All requests of one session")
    group.add_argument("--request", type=int, help="Full trail of one request")
    group.add_argument("--why", type=int, metavar="REQUEST_ID", help="Root-cause explanations of one request")
    group.add_argument("--stats", action="store_true", help="Outcome, root-cause and novelty summary")
    parser.add_argument("--json", action="store_true", help="JSON output for --request")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    display = Display(color=not args.no_color)

    if args.request is not None:
        trail = request_trail(args.request)
        if args.json:
            print(json.dumps(trail, indent=2, default=str))
        else:
            print_trail(trail, display)
        return 0
    if args.why is not None:
        trail = request_trail(args.why)
        display.out(f"  {trail['request']['response_headline']}")
        for item in trail["why_not"] or []:
            display.out(f"  {item['explanation_seq']}. [{item['stage']}] {item['code']}: {item['headline']}")
            display.out(display.c(f"     cause: {item['cause']}", "grey"))
            if item["fix"]:
                display.out(display.c(f"     fix  : {item['fix']}", "grey"))
        if not trail["why_not"]:
            display.out("  (no root causes recorded: the request succeeded)")
        return 0
    if args.stats:
        for title, sql in STATS_QUERIES.items():
            display.rule(title, "bold")
            columns, rows = fetch(sql)
            display.table(columns, rows, indent="  ", max_rows=40)
            display.out()
        return 0
    where, params = ("WHERE session_id = %s", (args.session,)) if args.session else ("", ())
    limit = "" if args.session else f"LIMIT {int(args.recent or 15)}"
    columns, rows = fetch(
        "SELECT request_id AS id, turn_no AS turn, LEFT(request_text, 45) AS request, action_type AS action, "
        "guardrail_verdict AS guard, effective_verdict AS effective, final_status AS status, attempts AS tries, "
        f"sandbox_runs AS runs, COALESCE(root_cause_code, '') AS root_cause FROM v_request_audit {where} "
        f"ORDER BY request_id {'ASC' if args.session else 'DESC'} {limit}",
        params,
    )
    display.table(columns, rows, indent="  ", max_rows=200)
    return 0


if __name__ == "__main__":
    sys.exit(main())
