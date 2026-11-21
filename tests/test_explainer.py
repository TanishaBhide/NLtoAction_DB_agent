"""Week 8: plain-English responses and "why not" root causes (no database needed).

Runs drive the real ReActAgent with fake planner / guardrails / executor so the
explanation is checked against the stop_reason the loop actually recorded.
"""

from __future__ import annotations

from database.executor import ExecutionError, ExecutionResult, PrecommitCheck
from database.explainer import ResponseBuilder, describe_rows, describe_write
from database.guardrails import GuardrailResult
from database.react_agent import ReActAgent
from database.session_memory import SessionMemory
from database.sql_generator import ActionPlan


def plan(sql="SELECT stock_code FROM products LIMIT 5", action="read", blocked=False, reason=None):
    return ActionPlan(action, "" if blocked else sql, ["products"], [], "x", 0.9,
                      "SELECT COUNT(*) FROM products" if action == "write" else None, blocked, reason)


class Gen:
    def __init__(self, *plans):
        self.plans, self.calls, self.last_parsed = list(plans), 0, None

    def generate(self, text, session_id=None, *, feedback=None, memory_context=None):
        self.calls += 1
        return self.plans[min(self.calls - 1, len(self.plans) - 1)]


class Guard:
    def __init__(self, result):
        self.result = result

    def evaluate(self, plan, **_):
        return self.result


class Exec:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def execute(self, plan, guardrail, **_):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


ALLOW = GuardrailResult("ALLOW", 0.0, reasons=["No Week 5 guardrail rule was triggered."])


def run(plans, results=(), guardrail=ALLOW, **kwargs):
    agent = ReActAgent(Gen(*plans), Guard(guardrail), Exec(*results), SessionMemory("t", persist=False),
                       explainer=ResponseBuilder(), **kwargs)
    return agent.run("request")


def error(sqlstate, message, retryable=False, **fields):
    return ExecutionError(sqlstate, "E", "x", message, retryable=retryable, explanation="explained", **fields)


# --------------------------------------------------------------- successes
def test_read_answer_is_a_sentence_about_the_rows():
    result = ExecutionResult("read_only", "read", "s", "ok", columns=["stock_code"], rows=[["A"], ["B"]], row_count=2)
    r = run([plan()], [result])
    assert r.stop_reason == "answered"
    assert r.response.headline == "Found 2 products." and not r.response.why_not


def test_single_value_and_grouped_results():
    assert describe_rows("invoices", ["count"], [[5]], 1)[0] == "The answer is 5."
    headline, summary = describe_rows("invoice_items", ["invoice_no", "n"], [["536365", 7], ["537967", 3]], 2,
                                      group_by=["invoice_no"])
    assert headline == "Here are 2 results, one per invoice_no."
    top, _ = describe_rows("customers", ["country", "customer_count"], [["United Kingdom", 84]], 1,
                           group_by=["country"])
    assert top == "Result: United Kingdom (customer_count 84)."
    assert "536365: 7; 537967: 3" in summary


def test_committed_write_mentions_change_transaction_and_undo():
    sql = "UPDATE invoice_items SET quantity = 12 WHERE invoice_no = '536365' AND stock_code = '85123A'"
    result = ExecutionResult("committed", "write", sql, "ok", affected_rows=1, transaction_id="777",
                             checks=[PrecommitCheck("row_limit", True, "")], compensation_sql=["UPDATE ..."],
                             audit_id=9)
    r = run([plan(sql, "write")], [result])
    assert r.stop_reason == "committed"
    assert r.response.headline == "Done: set quantity = 12 on 1 line item in invoice_items."
    assert "transaction 777" in r.response.summary
    assert any("compensation_ledger (audit #9)" in note for note in r.response.notes)


def test_describe_write_for_delete_and_insert():
    assert describe_write("DELETE FROM invoices WHERE invoice_no = '1'", 1).startswith("deleted 1 invoice from invoices")
    assert describe_write("INSERT INTO products (stock_code) VALUES ('X'), ('Y')", 2) == "added 2 products to products"


def test_retry_success_note_names_the_corrected_error():
    bad = ExecutionResult("failed", "read", "s", "f", error=error("42883", "operator does not exist", True))
    good = ExecutionResult("read_only", "read", "s", "ok", columns=["a"], rows=[[1]], row_count=1)
    r = run([plan("SELECT 1 LIMIT 1"), plan("SELECT 2 LIMIT 1")], [bad, good])
    assert r.status == "success" and r.attempts == 2
    assert "attempt 1 failed with SQLSTATE 42883" in r.response.notes[0]


def test_empty_result_explains_the_filter():
    empty = ExecutionResult("read_only", "read", "s", "ok", columns=["a"], rows=[], row_count=0)
    sql = "SELECT customer_id FROM customers WHERE country = 'Germany' LIMIT 5"
    r = run([plan(sql), plan(sql)], [empty, empty])
    assert r.stop_reason == "empty_confirmed"
    assert r.response.headline == "No customers match your request."
    assert r.response.why_not[0].code == "EMPTY_RESULT" and "country = 'Germany'" in r.response.why_not[0].cause


# ------------------------------------------------------------- why not ...
def test_planner_ddl_block():
    r = run([plan(blocked=True, reason="DDL is blocked; this week does not generate or execute DDL.")])
    assert r.stop_reason == "planner_block"
    why = r.response.why_not[0]
    assert why.stage == "planning" and why.code == "PLANNER_DDL" and why.fix
    assert r.response.headline == "I did not run this: schema changes (DDL) are never run by the agent."


def test_guardrail_block_lists_each_rule_with_a_fix():
    block = GuardrailResult("BLOCK", 100.0, triggered_rules=[
        {"rule_id": 10001, "rule_type": "WHERE_REQUIRED", "severity": "CRITICAL", "reason": "DELETE requires a WHERE clause."},
    ], reasons=["DELETE requires a WHERE clause."])
    r = run([plan("DELETE FROM products", "write")], guardrail=block)
    assert r.stop_reason == "guardrail_block"
    assert [w.code for w in r.response.why_not] == ["WHERE_REQUIRED"]
    assert "for invoice" in r.response.why_not[0].fix


def test_confirm_without_approval_explains_rule_and_approval():
    confirm = GuardrailResult("CONFIRM", 45.0, triggered_rules=[
        {"rule_id": 10003, "rule_type": "PII_COLUMN", "severity": "HIGH", "reason": "customer ids exposed"},
    ], reasons=["customer ids exposed"])
    r = run([plan()], guardrail=confirm)
    assert r.stop_reason == "not_approved"
    assert [w.code for w in r.response.why_not] == ["PII_COLUMN", "APPROVAL_NOT_GIVEN"]


def test_foreign_key_violation_names_referencing_table_and_fix():
    err = error("23503", "update or delete violates foreign key", constraint="fk_invoice_items_product",
                detail='Key (stock_code)=(84406B) is still referenced from table "invoice_items".')
    r = run([plan("DELETE FROM products WHERE stock_code = '84406B'", "write")],
            [ExecutionResult("failed", "write", "s", "f", error=err)])
    why = r.response.why_not[0]
    assert r.stop_reason == "execution_error" and why.code == "FK_VIOLATION"
    assert "84406B is still used by rows in invoice_items" in why.cause
    assert why.fix == "Delete or reassign the line items that reference it first."


def test_missing_parent_and_check_and_trigger_errors():
    builder = ResponseBuilder()
    fake_run = type("R", (), {"blast_radius": None})()
    missing = builder._error_explanation(error("23503", "m", constraint="fk_invoice_items_product",
                                               detail='Key (stock_code)=(NOPE) is not present in table "products".'),
                                         fake_run)
    assert missing.cause.startswith("There is no product with stock_code = NOPE")
    check = builder._error_explanation(error("23514", "m", constraint="chk_invoice_items_quantity"), fake_run)
    assert check.code == "CHECK_VIOLATION" and "can never be 0" in check.cause
    trig = builder._error_explanation(
        error("P0001", "negative unit_price is only allowed for adjustment invoices"), fake_run)
    assert trig.code == "TRIGGER_REJECTED" and "adjustment" in trig.cause


def test_precommit_invariant_quotes_rule_and_counts():
    rolled = ExecutionResult("rolled_back", "write", "s", "f", affected_rows=1, checks=[
        PrecommitCheck("invariant:negative_qty_only_on_cancellations", False, "x violations before=0 after=1")])
    r = run([plan("UPDATE invoice_items SET quantity = -3 WHERE invoice_item_id = 1", "write")], [rolled])
    why = r.response.why_not[0]
    assert r.stop_reason == "precommit_failed" and why.stage == "precommit"
    assert "Negative quantities appear only on cancelled invoices" in why.headline
    assert "from 0 to 1" in why.cause


def test_retry_budget_exhausted_keeps_last_error():
    bad = ExecutionResult("failed", "read", "s", "f", error=error("42883", "operator does not exist", True))
    r = run([plan()], [bad], max_attempts=2)
    assert r.stop_reason == "retry_exhausted"
    assert [w.code for w in r.response.why_not][:1] == ["RETRY_BUDGET_EXHAUSTED"]
    assert len(r.response.why_not[0].evidence["attempts"]) == 2


def test_dry_run_and_declined_commit():
    sql = "UPDATE invoice_items SET quantity = 2 WHERE invoice_item_id = 1"
    dry = ExecutionResult("rolled_back", "write", sql, "dry", affected_rows=1, dry_run=True)
    assert run([plan(sql, "write")], [dry]).response.why_not[0].code == "DRY_RUN"
    declined = ExecutionResult("rolled_back", "write", sql, "declined", affected_rows=1)
    assert run([plan(sql, "write")], [declined]).response.why_not[0].code == "COMMIT_DECLINED"


def test_response_text_renders_all_parts():
    r = run([plan(blocked=True, reason="Unscoped writes are blocked; provide a restrictive filter.")])
    text = r.response.text()
    assert text.splitlines()[0] == r.response.headline
    assert "Why not:" in text and "[planning]" in text and "What you can do:" in text
