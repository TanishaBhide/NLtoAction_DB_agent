"""NL-to-Action web app: chat with the agent, read its execution log, browse role-based views.

Standard library only (http.server); no extra packages beyond requirements.txt.

    python webapp/server.py                 # http://127.0.0.1:8000
    python webapp/server.py --port 8080
    python webapp/server.py --reset         # reset the demo database first

API
    GET  /api/status                         planner, database, table counts
    POST /api/chat     {session_id, message, dry_run}
    POST /api/approve  {session_id, pending_id, dry_run}   run the exact plan that needed approval
    GET  /api/views/user?page=0
    GET  /api/views/agent?page=0
    POST /api/views/admin {password, page}
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import sys
import threading
import traceback
import uuid
import warnings
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.audit_log import AuditLogger  # noqa: E402
from database.blast_radius import BlastRadiusAnalyzer  # noqa: E402
from database.db import DEMO_DATABASE_URL, connect, has_database_config  # noqa: E402
from database.env import load_env  # noqa: E402
from database.executor import SafeExecutor  # noqa: E402
from database.explainer import ResponseBuilder  # noqa: E402
from database.guardrails import GuardrailEngine  # noqa: E402
from database.intent_drift import IntentDriftDetector  # noqa: E402
from database.llm_client import LLMUnavailableError, create_llm_client  # noqa: E402
from database.react_agent import ReActAgent  # noqa: E402
from database.rule_planner import OfflinePlannerClient, sketch_sql  # noqa: E402
from database.session_memory import SessionMemory  # noqa: E402
from database.sql_generator import SQLGenerator  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
PAGE_SIZE = 5
MAX_RESULT_ROWS = 50

VIEWS: dict[str, dict[str, Any]] = {
    "user": {
        "title": "User view · product catalogue",
        "audience": "Shopper",
        "layout": "cards",
        "relation": "v_user_catalogue",
        "role": "dbm_reader",
        "columns": "popularity_rank, stock_code, product, price, units_sold, times_ordered, label",
        "order": "popularity_rank, stock_code",
        "description": "What a shopper may see: products they can buy, the usual price and how popular each one "
                       "is. No customers, invoices or other people's orders.",
    },
    "agent": {
        "title": "Customer agent view · customer accounts",
        "audience": "Customer-support agent",
        "layout": "table",
        "relation": "v_customer_agent_accounts",
        "role": "dbm_writer",
        "columns": "customer, country, orders, cancellations, total_spend, refunded, last_order, "
                   "days_since_last_order, support_flag",
        "order": "total_spend DESC, customer",
        "filter": "country",
        "description": "What a support agent needs to help a customer: one row per customer account with orders, "
                       "spend, refunds, recency and a support flag. Customer IDs are masked (last two digits only).",
    },
    "admin": {
        "title": "Database admin view · operations & audit",
        "audience": "Database administrator",
        "layout": "admin",
        "relation": "v_dba_activity",
        "role": "dbm_admin",
        "columns": "request_id, created_at, request_text, final_sql, guardrail_verdict, effective_verdict, "
                   "final_status, root_cause_code",
        "order": "request_id DESC",
        "protected": True,
        "description": "What the DBA monitors: storage and row counts per table, business-rule health, what the "
                       "agent has been doing, and the latest audited requests with their exact SQL.",
    },
}


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ------------------------------------------------------------- run -> JSON
def step_details(step: Any) -> list[dict[str, Any]]:
    data, lines = step.data, []
    if data.get("memory_context"):
        lines += [{"type": "text", "text": "memory: " + line} for line in data["memory_context"].splitlines()]
    if data.get("feedback"):
        lines += [{"type": "warn", "text": line} for line in data["feedback"].splitlines()]
    plan = data.get("plan")
    if plan is not None and not plan.blocked:
        lines.append({"type": "sql", "text": plan.sql})
        if plan.count_sql:
            lines.append({"type": "sql", "text": plan.count_sql, "label": "row-count estimate"})
    guardrail = data.get("guardrail")
    if guardrail is not None:
        for rule in guardrail.triggered_rules:
            lines.append({"type": "warn", "text": f"rule {rule['rule_type']} ({rule['severity']}): {rule['reason']}"})
        lines += [{"type": "text", "text": reason} for reason in guardrail.reasons
                  if reason not in {r["reason"] for r in guardrail.triggered_rules}]
    blast = data.get("blast_radius")
    if blast is not None:
        for impact in blast.impacts:
            if not impact.effect.startswith("direct"):
                lines.append({"type": "error" if impact.effect == "restrict" else "warn", "text": impact.describe()})
        lines += [{"type": "text", "text": "trigger fires: " + t} for t in blast.triggers]
        if blast.predicted_failure:
            lines.append({"type": "error",
                          "text": f"predicted failure {blast.predicted_sqlstate}: {blast.predicted_failure}"})
    drift = data.get("drift")
    if drift is not None:
        lines += [{"type": "text", "text": f"{name} = {value:.2f}"} for name, value in drift.features.items() if value]
        lines += [{"type": "warn" if drift.escalate else "text", "text": r} for r in drift.reasons]
    execution = data.get("execution")
    if execution is not None:
        for tx in execution.tx_log:
            lines.append({"type": "tx", "text": tx.sql, "outcome": tx.outcome, "ms": tx.ms})
        for check in execution.checks:
            lines.append({"type": "check", "ok": check.passed, "text": f"{check.name}: {check.detail}"})
        if execution.error:
            error = execution.error
            lines.append({"type": "error", "text": f"SQLSTATE {error.sqlstate} {error.error_name}: {error.message}"})
            for label, value in (("detail", error.detail), ("hint", error.hint), ("constraint", error.constraint)):
                if value:
                    lines.append({"type": "error", "text": f"{label}: {value}"})
    return lines


EXECUTION_OUTCOME = {
    "read_only": ("executed", "Executed in a READ ONLY transaction (then rolled back: nothing to commit)"),
    "committed": ("committed", "Executed and COMMITTED"),
    "rolled_back": ("rolled_back", "Executed in the sandbox, then ROLLED BACK"),
    "failed": ("failed", "Sent to PostgreSQL and REJECTED; transaction rolled back"),
    "not_executed": ("blocked", "Refused by the execution gate; never sent"),
}


def sql_log(run: Any) -> list[dict[str, Any]]:
    """Every SQL statement the agent produced for this request, executed or not."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for step in run.steps:
        data = step.data
        plan = data.get("plan")
        if plan is not None:
            if plan.blocked:
                sketch = sketch_sql(run.request, run.parsed_intent) if run.parsed_intent else None
                entries.append({
                    "attempt": step.attempt, "sql": sketch, "source": "reconstructed" if sketch else "none",
                    "state": "blocked", "verdict": None,
                    "outcome": "Blocked by the planner before any SQL was generated; never sent to the database",
                    "reason": plan.block_reason,
                })
                current = None
            else:
                current = {"attempt": step.attempt, "sql": plan.sql, "count_sql": plan.count_sql,
                           "source": "planner", "state": "planned", "verdict": None,
                           "outcome": "Planned", "reason": plan.explanation}
                entries.append(current)
        elif current is not None and data.get("guardrail") is not None:
            guardrail = data["guardrail"]
            current["verdict"] = guardrail.verdict
            current["risk"] = guardrail.risk_score
            if guardrail.verdict == "BLOCK":
                current.update(state="blocked", outcome="Blocked by the Week 5 guardrails; never sent",
                               reason="; ".join(guardrail.reasons))
        elif current is not None and "approved" in data:
            current["approved"] = data["approved"]
            if not data["approved"]:
                current.update(state="approval", outcome="Awaiting approval; not executed",
                               reason="; ".join(e["reason"] for e in run.escalations) or
                               ("; ".join(run.guardrail.reasons) if run.guardrail else ""))
        elif current is not None and data.get("execution") is not None:
            result = data["execution"]
            state, outcome = EXECUTION_OUTCOME.get(result.status, (result.status, result.status))
            if result.dry_run and result.status == "rolled_back" and not result.error:
                outcome = "Dry run: executed in the sandbox, then rolled back on purpose"
            current.update(state=state, outcome=outcome, executed_sql=result.executed_sql,
                           sqlstate=result.error.sqlstate if result.error else None,
                           reason=result.reason, transaction_id=result.transaction_id,
                           affected_rows=result.affected_rows, row_count=result.row_count)
    last = next((e for e in reversed(entries) if e.get("verdict")), None)
    if last is not None and run.effective_verdict and run.effective_verdict != last["verdict"]:
        last["effective_verdict"] = run.effective_verdict  # Week 9 escalation
    if not entries and run.parsed_intent:
        sketch = sketch_sql(run.request, run.parsed_intent)
        entries.append({"attempt": 1, "sql": sketch, "source": "reconstructed" if sketch else "none",
                        "state": "blocked", "outcome": "Never sent to the database", "reason": run.answer})
    return entries


def access_summary(run: Any) -> dict[str, Any]:
    """Plain answer to: may I modify the database with this request, and why (not)?"""
    action = (run.plan.action_type if run.plan else None) or run.parsed_intent.get("action_type") or "unknown"
    why = [f"{item.headline}: {item.cause}" for item in (run.response.why_not if run.response else [])]
    guardrail = run.guardrail
    verdict_line = (f"Week 5 guardrails returned {guardrail.verdict} (risk {guardrail.risk_score:g}/100)"
                    + (f"; Week 9 escalated it to {run.effective_verdict}"
                       if run.effective_verdict and run.effective_verdict != guardrail.verdict else "")
                    if guardrail else None)
    result = run.execution
    if action == "read":
        if run.status == "success":
            return {"state": "read", "title": "Read-only access: nothing was modified",
                    "reasons": ["You asked to read data, not to change it.",
                                "The query ran inside a READ ONLY transaction, so PostgreSQL itself would have "
                                "refused any modification.", *(filter(None, [verdict_line]))]}
        if run.status == "needs_approval":
            return {"state": "approval", "title": "Read access needs approval",
                    "reasons": [*filter(None, [verdict_line]), *why]}
        return {"state": "denied", "title": "The read was not completed", "reasons": [*filter(None, [verdict_line]), *why]}
    if action in {"write", "ddl"}:
        if run.status == "success" and result is not None and result.status == "committed":
            reasons = [*filter(None, [verdict_line]),
                       f"All {len(result.checks)} pre-commit checks passed (constraints, row limits, business "
                       "invariants).",
                       f"The change was committed in transaction {result.transaction_id}."]
            if run.blast_radius and not run.blast_radius.error:
                reasons.append("Blast-radius preview: " + run.blast_radius.summary())
            if result.compensation_sql:
                reasons.append(f"Undo SQL is stored in compensation_ledger (audit #{result.audit_id}).")
            return {"state": "granted", "title": "Yes: you had write access for this change", "reasons": reasons}
        if run.stop_reason == "dry_run":
            return {"state": "dryrun", "title": "Write allowed, but this was a dry run (rolled back)",
                    "reasons": [*filter(None, [verdict_line]), *why]}
        if run.status == "needs_approval":
            return {"state": "approval", "title": "Not yet: this modification needs your approval",
                    "reasons": [*filter(None, [verdict_line]), *why]}
        title = {
            "execution_error": "No: the database rejected this modification (nothing was changed)",
            "retry_exhausted": "No: no working SQL could be produced (nothing was changed)",
            "precommit_failed": "No: the change breaks a business rule, so it was rolled back",
            "commit_declined": "No: the commit was declined (nothing was changed)",
            "gate_refused": "No: the execution gate refused this statement",
        }.get(run.stop_reason, "No: you do not have access to make this modification")
        return {"state": "denied", "title": title, "reasons": [*filter(None, [verdict_line]), *why]}
    return {"state": "denied", "title": "Request not understood", "reasons": why or [run.answer]}


def serialize_run(run: Any, *, planner: str, pending_id: str | None) -> dict[str, Any]:
    result = run.execution
    payload: dict[str, Any] = {
        "request": run.request,
        "session_id": run.session_id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "attempts": run.attempts,
        "latency_ms": run.latency_ms,
        "planner": planner,
        "request_id": run.request_id,
        "headline": run.response.headline if run.response else run.answer,
        "summary": run.response.summary if run.response else "",
        "why_not": [vars(item) for item in run.response.why_not] if run.response else [],
        "notes": run.response.notes if run.response else [],
        "technical": run.answer,
        "access": access_summary(run),
        "sql_log": sql_log(run),
        "steps": [{"index": s.index, "attempt": s.attempt, "kind": s.kind, "text": s.text,
                   "details": step_details(s)} for s in run.steps],
        "pending_id": pending_id,
        "result": None,
    }
    if result is not None and result.columns and result.rows and result.status in {"read_only", "committed",
                                                                                    "rolled_back"}:
        payload["result"] = {
            "columns": result.columns,
            "rows": result.rows[:MAX_RESULT_ROWS],
            "row_count": result.row_count,
            "shown": min(result.row_count, MAX_RESULT_ROWS),
            "kind": "rows" if result.action_type == "read" else "affected rows (RETURNING *)",
        }
    return jsonable(payload)


# ------------------------------------------------------------------ agent
class PinnedGenerator:
    """Returns the exact plan the user approved on the first attempt, then delegates (for retries)."""

    def __init__(self, inner: Any, plan: Any, parsed: Any) -> None:
        self.inner, self.plan, self.parsed, self.used = inner, plan, parsed, False

    @property
    def last_parsed(self) -> Any:
        return self.parsed if not self.used or self.inner.last_parsed is None else self.inner.last_parsed

    @property
    def prompt_builder(self) -> Any:
        return self.inner.prompt_builder

    def generate(self, text: str, session_id: str | None = None, **kwargs: Any) -> Any:
        if not self.used:
            self.used = True
            return self.plan
        return self.inner.generate(text, session_id=session_id, **kwargs)


class ApprovalExpired(Exception):
    """The pending approval id is unknown or was already used."""


class FallbackPlannerClient:
    """The live LLM plans; if the provider is unreachable (network, timeout, rate limit) the offline
    rule-based planner answers that one request instead, and the plan explanation says so."""

    def __init__(self, live: Any, offline: OfflinePlannerClient, label: str) -> None:
        self.live, self.offline, self.label = live, offline, label

    def complete(self, system: str, user: str, tool_schema: dict, *, context: str | None = None) -> dict:
        try:
            plan = self.live.complete(system, user, tool_schema, context=context)
            if isinstance(plan, dict) and isinstance(plan.get("explanation"), str) and plan["explanation"].strip():
                plan["explanation"] = f"[{self.label}] {plan['explanation']}"
            return plan
        except LLMUnavailableError as error:
            plan = self.offline.complete(system, user, tool_schema, context=context)
            reason = str(error).strip().splitlines()[0][:160] if str(error).strip() else "unavailable"
            plan["explanation"] = f"[LLM unavailable: {reason}; offline planner used] {plan.get('explanation', '')}"
            return plan


class AgentService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.agents: dict[str, ReActAgent] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.offline = OfflinePlannerClient()
        self.client: Any = self.offline
        self.planner = OfflinePlannerClient.name
        self.planner_note: str | None = None
        forced_offline = os.environ.get("PLANNER", "").lower() == "offline"
        provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
        if forced_offline:
            self.planner_note = "PLANNER=offline is set"
        elif os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY"):
            try:
                live = create_llm_client(provider)
                model = (os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b") if provider == "groq"
                         else os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
                label = f"{'Groq' if provider == 'groq' else provider} LLM · {model}"
                self.client, self.planner = FallbackPlannerClient(live, self.offline, label), label
            except (TypeError, ValueError) as error:
                self.planner_note = f"LLM not configured ({error})"
        else:
            self.planner_note = "no GROQ_API_KEY in the environment or .env"

    @property
    def live(self) -> bool:
        return self.client is not self.offline

    def agent(self, session_id: str) -> ReActAgent:
        if session_id not in self.agents:
            client = self.client
            self.agents[session_id] = ReActAgent(
                SQLGenerator(llm_client=client), GuardrailEngine(), SafeExecutor(), SessionMemory(session_id),
                explainer=ResponseBuilder(), auditor=AuditLogger(), blast_analyzer=BlastRadiusAnalyzer(),
                drift_detector=IntentDriftDetector(),
            )
        return self.agents[session_id]

    def chat(self, session_id: str, message: str, dry_run: bool) -> dict[str, Any]:
        with self.lock:
            agent = self.agent(session_id)
            agent.dry_run, agent.approver = dry_run, None
            run = agent.run(message)
            pending_id = None
            if run.status == "needs_approval" and run.plan is not None:
                pending_id = uuid.uuid4().hex[:12]
                self.pending[session_id] = {"id": pending_id, "request": message, "plan": run.plan,
                                            "parsed": agent.generator.last_parsed}
            return serialize_run(run, planner=self.planner, pending_id=pending_id)

    def approve(self, session_id: str, pending_id: str, dry_run: bool) -> dict[str, Any]:
        with self.lock:
            pending = self.pending.get(session_id)
            if not pending or pending["id"] != pending_id:
                raise ApprovalExpired("This approval has expired; send the request again.")
            del self.pending[session_id]
            agent = self.agent(session_id)
            inner = agent.generator
            agent.generator = PinnedGenerator(inner, pending["plan"], pending["parsed"])
            agent.approver, agent.dry_run = (lambda plan, guardrail: True), dry_run
            try:
                run = agent.run(pending["request"])
            finally:
                agent.generator, agent.approver = inner, None
            return serialize_run(run, planner=self.planner + " · human-approved plan", pending_id=None)


# ------------------------------------------------------------------ views
def query(sql: str, params: tuple = ()) -> tuple[list[str], list[list[Any]]]:
    connection = connect()
    try:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(sql, params or None)  # no params: '%' in LIKE patterns stays literal
            return [d[0] for d in cursor.description], [list(r) for r in cursor.fetchall()]
    finally:
        connection.rollback()
        connection.close()


def view_page(key: str, page: int, country: str | None = None) -> dict[str, Any]:
    spec = VIEWS[key]  # relation and column names come from this whitelist only
    page = max(0, page)
    where, params = "", ()
    if country and spec.get("filter") == "country":
        where, params = "WHERE country = %s", (country,)
    _, total = query(f"SELECT COUNT(*) FROM {spec['relation']} {where}", params)
    sql = (f"SELECT {spec['columns']} FROM {spec['relation']} {where} ORDER BY {spec['order']} "
           f"LIMIT {PAGE_SIZE} OFFSET {page * PAGE_SIZE}")
    columns, rows = query(sql, params)
    payload: dict[str, Any] = {
        "key": key, "title": spec["title"], "audience": spec["audience"], "layout": spec["layout"],
        "description": spec["description"], "relation": spec["relation"], "role": spec["role"],
        "sql": sql.replace("%s", f"'{country}'") if country else sql, "columns": columns, "rows": rows,
        "page": page, "page_size": PAGE_SIZE, "total": total[0][0], "country": country,
    }
    if spec.get("filter") == "country":
        _, countries = query(f"SELECT country, COUNT(*) FROM {spec['relation']} GROUP BY country ORDER BY 2 DESC, 1")
        payload["countries"] = [{"name": c, "count": n} for c, n in countries]
    if key == "admin":
        payload["dashboard"] = admin_dashboard()
    return jsonable(payload)


def admin_dashboard() -> dict[str, Any]:
    """Live operational figures only a DBA sees."""
    from database.executor import INVARIANTS

    _, storage = query(
        "SELECT c.relname, pg_total_relation_size(c.oid), "
        "CASE WHEN c.relname IN ('customers', 'products', 'invoices', 'invoice_items') THEN 'operational' "
        "ELSE 'agent' END "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' ORDER BY 3 DESC, 2 DESC")
    tables = []
    for name, size_bytes, kind in storage:  # exact counts: the demo database is small
        _, rows = query('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"')
        tables.append({"table": name, "rows": rows[0][0], "size_kb": round(size_bytes / 1024), "kind": kind})
    _, size = query("SELECT pg_database_size(current_database()), current_database(), version()")
    invariants = []
    for name, (description, sql) in INVARIANTS.items():
        _, rows = query(sql)
        invariants.append({"name": name, "description": description, "violations": rows[0][0]})
    _, by_status = query("SELECT final_status, COUNT(*) FROM agent_requests GROUP BY 1 ORDER BY 2 DESC")
    _, by_verdict = query("SELECT COALESCE(effective_verdict, 'none'), COUNT(*) FROM agent_requests GROUP BY 1 "
                          "ORDER BY 2 DESC")
    _, executed = query("SELECT execution_status, COUNT(*) FROM execution_audit GROUP BY 1 ORDER BY 2 DESC")
    _, escalations = query("SELECT COUNT(*) FROM agent_requests WHERE jsonb_array_length(escalations) > 0")
    _, sessions = query("SELECT COUNT(DISTINCT session_id) FROM agent_requests")
    return {
        "database": size[0][1], "database_size_kb": round(size[0][0] / 1024),
        "server_version": size[0][2].split(",")[0], "tables": tables, "invariants": invariants,
        "requests_by_status": dict(by_status), "requests_by_verdict": dict(by_verdict),
        "executions_by_status": dict(executed), "escalated_requests": escalations[0][0],
        "agent_sessions": sessions[0][0], "requests": sum(n for _, n in by_status),
    }


def status() -> dict[str, Any]:
    info: dict[str, Any] = {"planner": SERVICE.planner, "live_llm": SERVICE.live, "planner_note": SERVICE.planner_note,
                            "database": False}
    try:
        _, rows = query("SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM products), "
                        "(SELECT COUNT(*) FROM invoices), (SELECT COUNT(*) FROM invoice_items), "
                        "to_regclass('v_dba_activity') IS NOT NULL")
        counts = rows[0]
        info.update(database=True, views_ready=bool(counts[4]),
                    tables={"customers": counts[0], "products": counts[1], "invoices": counts[2],
                            "invoice_items": counts[3]})
    except Exception as error:  # noqa: BLE001
        info["error"] = f"{type(error).__name__}: {str(error).strip().splitlines()[0] if str(error).strip() else ''}"
    return info


def admin_password() -> str:
    return os.environ.get("WEB_ADMIN_PASSWORD", "password")


SERVICE: AgentService


# ------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "NL2ActionWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _session(self, body: dict[str, Any]) -> str:
        session = str(body.get("session_id") or "")
        if not session or len(session) > 36:
            raise ValueError("missing session_id")
        return session

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            if url.path == "/api/status":
                return self._json(status())
            if url.path.startswith("/api/views/"):
                key = url.path.rsplit("/", 1)[-1]
                if key not in VIEWS:
                    return self._json({"error": "unknown view"}, 404)
                if VIEWS[key].get("protected"):
                    return self._json({"error": "password required"}, 401)
                params = parse_qs(url.query)
                page = int(params.get("page", ["0"])[0])
                country = (params.get("country", [""])[0] or None)
                return self._json(view_page(key, page, country[:100] if country else None))
            return self._static(url.path)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": f"{type(error).__name__}: {error}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path == "/api/chat":
                message = str(body.get("message") or "").strip()
                if not message:
                    return self._json({"error": "empty message"}, 400)
                return self._json(SERVICE.chat(self._session(body), message[:1000], bool(body.get("dry_run"))))
            if url.path == "/api/approve":
                return self._json(SERVICE.approve(self._session(body), str(body.get("pending_id") or ""),
                                                  bool(body.get("dry_run"))))
            if url.path == "/api/views/admin":
                if not hmac.compare_digest(str(body.get("password") or ""), admin_password()):
                    return self._json({"error": "Wrong password."}, 401)
                return self._json(view_page("admin", int(body.get("page") or 0)))
            return self._json({"error": "not found"}, 404)
        except ApprovalExpired as error:
            return self._json({"error": str(error)}, 409)
        except ValueError as error:
            return self._json({"error": str(error)}, 400)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": f"{type(error).__name__}: {error}"}, 500)

    def _static(self, path: str) -> None:
        name = "index.html" if path in {"", "/"} else path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        return self._send(200, target.read_bytes(), content_type)


class AppServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second server bind the same port silently while the
    # first one keeps answering; fail loudly instead.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def ensure_schema() -> None:
    """Create the web views if the Week 8 audit tables exist but the views do not."""
    try:
        _, rows = query("SELECT to_regclass('v_request_audit') IS NOT NULL, to_regclass('v_dba_activity') IS NOT NULL")
    except Exception as error:  # noqa: BLE001
        print(f"  ! PostgreSQL is not reachable ({error}).\n    Start it and run: python database/setup_demo_db.py")
        return
    has_audit, has_views = rows[0]
    if not has_audit:
        print("  ! The agent tables are missing. Run: python database/setup_demo_db.py")
        return
    if not has_views:
        connection = connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute((ROOT / "database" / "agent_schema_web.sql").read_text(encoding="utf-8"))
            print("  created the role-based views (database/agent_schema_web.sql)")
        finally:
            connection.close()


def main(argv: list[str] | None = None) -> int:
    global SERVICE
    parser = argparse.ArgumentParser(description="NL-to-Action web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true", help="Reset the demo database (schema + sample data) first")
    args = parser.parse_args(argv)
    loaded = load_env()  # GROQ_API_KEY / LLM_PROVIDER / DATABASE_URL from .env (names only are printed)
    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    if args.reset:
        from database.setup_demo_db import apply_all

        apply_all()
    ensure_schema()
    SERVICE = AgentService()
    try:
        server = AppServer((args.host, args.port), Handler)
    except OSError as error:
        print(f"  ! Port {args.port} is already in use ({error}). Is the app already running? "
              f"Stop it, or start with --port {args.port + 1}.")
        return 1
    print(f"\n  NL-to-Action web app  ->  http://{args.host}:{args.port}")
    if loaded:
        print(f"  loaded from .env: {', '.join(loaded)}")
    print(f"  planner: {SERVICE.planner}" + (f"  ({SERVICE.planner_note})" if SERVICE.planner_note else ""))
    print("  admin view password: " + ("(from WEB_ADMIN_PASSWORD)" if os.environ.get("WEB_ADMIN_PASSWORD")
                                       else "password"))
    print("  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
