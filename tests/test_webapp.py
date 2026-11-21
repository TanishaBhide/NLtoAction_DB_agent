"""Web app API (webapp/server.py).

Runs the real HTTP server in a thread on a free port. Integration tests need
PostgreSQL with the Week 8/9 tables and are skipped otherwise; they only use
requests that do not change data (reads, blocked or unapproved writes, dry runs).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
import uuid

import pytest

from database.db import DEMO_DATABASE_URL, connect


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL") or DEMO_DATABASE_URL
    try:
        connection = connect(url, connect_timeout=2)
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('agent_requests') IS NOT NULL")
            ready = cursor.fetchone()[0]
        connection.close()
        return url if ready else None
    except Exception:
        return None


DB_URL = _database_url()


@pytest.fixture(scope="module")
def base_url():
    saved = {key: os.environ.get(key) for key in ("DATABASE_URL", "PLANNER")}
    os.environ["DATABASE_URL"] = DB_URL or "postgresql://nobody@127.0.0.1:1/x"
    os.environ["PLANNER"] = "offline"
    from webapp import server

    if DB_URL:
        server.ensure_schema()
    server.SERVICE = server.AgentService()
    httpd = server.AppServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        if DB_URL:  # remove this module's test sessions (audit rows cascade)
            connection = connect(DB_URL)
            try:
                with connection, connection.cursor() as cursor:
                    cursor.execute("DELETE FROM schema_lookup_log WHERE session_id LIKE 'test-web-%'")
                    cursor.execute("DELETE FROM agent_sessions WHERE session_id LIKE 'test-web-%'")
            finally:
                connection.close()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_static_files_and_traversal(base_url):
    status, body = call(base_url, "/")
    assert status == 200 and b"NL-to-Action DB Agent" in body
    assert call(base_url, "/static/app.js")[0] == 200
    assert call(base_url, "/static/../server.py")[0] == 404
    assert call(base_url, "/static/%2e%2e/server.py")[0] == 404


def test_admin_view_needs_the_password(base_url):
    assert call(base_url, "/api/views/admin")[0] == 401
    status, body = call(base_url, "/api/views/admin", {"password": "wrong"})
    assert status == 401 and json.loads(body)["error"] == "Wrong password."


def test_bad_requests(base_url):
    assert call(base_url, "/api/chat", {"session_id": "s", "message": "  "})[0] == 400
    assert call(base_url, "/api/chat", {"message": "show customers"})[0] == 400
    assert call(base_url, "/api/views/nope")[0] == 404


needs_db = pytest.mark.skipif(DB_URL is None, reason="PostgreSQL with the Week 8/9 tables is not reachable")


def chat(base, session, message, **extra):
    status, body = call(base, "/api/chat", {"session_id": session, "message": message, **extra})
    assert status == 200, body
    return json.loads(body)


@needs_db
def test_read_shows_sql_access_log_and_rows(base_url):
    run = chat(base_url, f"test-web-{uuid.uuid4().hex[:8]}", "show customers from France")
    assert run["status"] == "success" and run["access"]["state"] == "read"
    entry = run["sql_log"][0]
    assert entry["state"] == "executed" and "WHERE customers.country = 'France'" in entry["sql"]
    kinds = [step["kind"] for step in run["steps"]]
    assert kinds[0] == "thought" and kinds[-1] == "answer" and "observation" in kinds
    tx = [d["text"] for s in run["steps"] for d in s["details"] if d["type"] == "tx"]
    assert tx[0].startswith("BEGIN ISOLATION LEVEL READ COMMITTED READ ONLY") and tx[-1] == "ROLLBACK"
    assert run["result"]["row_count"] >= 1


@needs_db
def test_blocked_ddl_still_shows_the_sql_it_would_have_run(base_url):
    run = chat(base_url, f"test-web-{uuid.uuid4().hex[:8]}", "drop the invoices table")
    assert run["status"] == "blocked" and run["access"]["state"] == "denied"
    entry = run["sql_log"][0]
    assert entry["sql"] == "DROP TABLE invoices" and entry["source"] == "reconstructed"
    assert entry["state"] == "blocked" and "never sent" in entry["outcome"]


@needs_db
def test_drift_needs_approval_then_approved_plan_runs_as_dry_run(base_url):
    session = f"test-web-{uuid.uuid4().hex[:8]}"
    chat(base_url, session, "show the line items of invoice 537967")
    run = chat(base_url, session, "delete them")
    assert run["status"] == "needs_approval" and run["access"]["state"] == "approval" and run["pending_id"]
    assert run["sql_log"][0]["state"] == "approval" and run["sql_log"][0]["effective_verdict"] == "CONFIRM"
    status, body = call(base_url, "/api/approve", {"session_id": session, "pending_id": run["pending_id"],
                                                   "dry_run": True})
    approved = json.loads(body)
    assert status == 200
    assert approved["sql_log"][0]["sql"] == run["sql_log"][0]["sql"]       # the exact plan that was approved
    assert approved["stop_reason"] in {"precommit_failed", "dry_run"}     # never committed
    assert call(base_url, "/api/approve", {"session_id": session, "pending_id": run["pending_id"]})[0] == 409


@needs_db
def test_views_return_five_records_per_page(base_url):
    for key in ("user", "agent"):
        status, body = call(base_url, f"/api/views/{key}?page=0")
        view = json.loads(body)
        assert status == 200 and len(view["rows"]) <= 5 and view["page_size"] == 5
    status, body = call(base_url, "/api/views/admin", {"password": "password", "page": 0})
    view = json.loads(body)
    assert status == 200 and view["relation"] == "v_dba_activity" and len(view["rows"]) <= 5
    customers = json.loads(call(base_url, "/api/views/agent?page=0")[1])
    masked = [row[customers["columns"].index("customer")] for row in customers["rows"]]
    assert all(value.startswith("*") for value in masked)


@needs_db
def test_the_three_views_are_different(base_url):
    user = json.loads(call(base_url, "/api/views/user?page=0")[1])
    agent = json.loads(call(base_url, "/api/views/agent?page=0")[1])
    admin = json.loads(call(base_url, "/api/views/admin", {"password": "password", "page": 0})[1])
    assert {user["layout"], agent["layout"], admin["layout"]} == {"cards", "table", "admin"}
    assert len({user["relation"], agent["relation"], admin["relation"]}) == 3
    # no column is shared between the shopper's catalogue and the agent's customer accounts
    assert not set(user["columns"]) & set(agent["columns"])
    assert "customer" not in user["columns"] and "price" in user["columns"]
    assert "support_flag" in agent["columns"] and "final_sql" in admin["columns"]
    dashboard = admin["dashboard"]
    assert {t["table"] for t in dashboard["tables"]} >= {"customers", "invoice_items", "agent_requests"}
    assert len(dashboard["invariants"]) == 4 and dashboard["database_size_kb"] > 0
    if agent["countries"]:
        country = agent["countries"][0]["name"]
        filtered = json.loads(call(base_url, f"/api/views/agent?page=0&country={country.replace(' ', '%20')}")[1])
        assert filtered["total"] == agent["countries"][0]["count"]
        assert all(row[filtered["columns"].index("country")] == country for row in filtered["rows"])
