"""Week 7: ReAct loop, bounded retries, and session memory (no database needed)."""

from __future__ import annotations

from database.demo_llm import ScriptedLLMClient
from database.executor import ExecutionError, ExecutionResult, PrecommitCheck
from database.guardrails import GuardrailResult
from database.react_agent import ReActAgent
from database.session_memory import SessionMemory
from database.sql_generator import ActionPlan, SQLGenerator


def read_plan(sql: str = "SELECT 1 LIMIT 1", blocked: bool = False) -> ActionPlan:
    return ActionPlan("read", sql, ["t"], [], "x", 0.9, None, blocked, "nope" if blocked else None)


class FakeGenerator:
    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []
        self.last_parsed = None

    def generate(self, text, session_id=None, *, feedback=None, memory_context=None):
        self.calls.append({"text": text, "feedback": feedback, "memory": memory_context})
        return self.plans[min(len(self.calls) - 1, len(self.plans) - 1)]


class FakeGuardrails:
    def __init__(self, verdict="ALLOW"):
        self.verdict = verdict

    def evaluate(self, plan, **_):
        return GuardrailResult(self.verdict, 0.0, reasons=["test"])


class FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def execute(self, plan, guardrail, **kwargs):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


def ok(rows=1):
    return ExecutionResult("read_only", "read", "SELECT 1", "ok", columns=["a"], rows=[[1]] * rows, row_count=rows)


def failed(sqlstate="42883", retryable=True, category="syntax_or_semantic"):
    error = ExecutionError(sqlstate, "E", category, "operator does not exist", retryable=retryable, explanation="x")
    return ExecutionResult("failed", "read", "SELECT 1", "failed", error=error)


def agent(plans, results, verdict="ALLOW", **kwargs):
    return ReActAgent(
        FakeGenerator(plans), FakeGuardrails(verdict), FakeExecutor(results),
        SessionMemory("unit-test", persist=False), **kwargs,
    )


def test_retryable_error_is_fed_back_and_second_attempt_succeeds():
    a = agent([read_plan("SELECT bad LIMIT 1"), read_plan("SELECT good LIMIT 1")], [failed(), ok()])
    run = a.run("show x")
    assert run.status == "success" and run.attempts == 2
    feedback = a.generator.calls[1]["feedback"]
    assert "Previous attempts: 1" in feedback and "42883" in feedback and "SELECT bad" in feedback
    kinds = [step.kind for step in run.steps]
    assert kinds[0] == "thought" and kinds[-1] == "answer"
    assert {"thought", "action", "observation"} <= set(kinds)


def test_non_retryable_error_stops_immediately():
    a = agent([read_plan()], [failed("23514", False, "integrity_violation")])
    run = a.run("set quantity to 0")
    assert run.status == "failed" and run.attempts == 1 and a.executor.calls == 1


def test_retries_are_bounded():
    a = agent([read_plan()], [failed()], max_attempts=3)
    run = a.run("show x")
    assert run.status == "failed" and run.attempts == 3 and a.executor.calls == 3
    assert "retry budget" in run.steps[-2].text


def test_empty_result_gets_exactly_one_feedback_retry():
    a = agent([read_plan("SELECT a LIMIT 1"), read_plan("SELECT b LIMIT 1")], [ok(0), ok(0)])
    run = a.run("find x")
    assert run.status == "success" and run.attempts == 2 and a.executor.calls == 2
    assert "returned 0 rows" in a.generator.calls[1]["feedback"]


def test_guardrail_block_is_final_and_never_executes():
    a = agent([read_plan()], [ok()], verdict="BLOCK")
    run = a.run("drop everything")
    assert run.status == "blocked" and a.executor.calls == 0 and len(a.generator.calls) == 1


def test_planner_block_is_final():
    a = agent([read_plan(blocked=True)], [ok()])
    assert a.run("drop the invoices table").status == "blocked" and a.executor.calls == 0


def test_confirm_without_approval_is_not_executed():
    a = agent([read_plan()], [ok()], verdict="CONFIRM")
    run = a.run("x")
    assert run.status == "needs_approval" and a.executor.calls == 0


def test_confirm_with_approval_executes():
    a = agent([read_plan()], [ok()], verdict="CONFIRM", approver=lambda plan, guardrail: True)
    assert a.run("x").status == "success" and a.executor.calls == 1


def test_precommit_failure_is_not_retried():
    rolled = ExecutionResult("rolled_back", "write", "UPDATE", "check failed",
                             checks=[PrecommitCheck("invariant:x", False, "before=0 after=1")])
    a = agent([read_plan()], [rolled])
    run = a.run("x")
    assert run.status == "rolled_back" and a.executor.calls == 1 and "invariant:x" in run.answer


def test_session_memory_records_turns_and_feeds_context_to_next_request():
    a = agent([read_plan("SELECT customer_id FROM customers LIMIT 1")], [ok()])
    a.run("show customers")
    a.run("only the ones from France")
    assert [turn.request for turn in a.memory.turns] == ["show customers", "only the ones from France"]
    assert a.generator.calls[0]["memory"] is None
    memory = a.generator.calls[1]["memory"]
    assert 'Turn 1: "show customers"' in memory and "SELECT customer_id" in memory
    assert a.memory.event_seq >= 2 * 8  # every thought/action/observation/answer is logged as an event


def test_generator_passes_feedback_and_memory_into_the_llm_prompt():
    client = ScriptedLLMClient()
    generator = SQLGenerator(llm_client=client)
    plan = generator.generate("show invoices for customer 17850", memory_context="Turn 1: earlier",
                              feedback="Previous attempts: 1\nPostgreSQL error 42883")
    assert "customer_id = '17850'" in plan.sql  # scripted attempt 2
    prompt = client.calls[-1]["user_prompt"]
    assert "SESSION MEMORY" in prompt and "Turn 1: earlier" in prompt
    assert "EXECUTION FEEDBACK" in prompt and "42883" in prompt


def test_scripted_llm_blocks_unknown_requests():
    plan = SQLGenerator(llm_client=ScriptedLLMClient()).generate("show customers from Germany")
    assert plan.blocked and "GROQ_API_KEY" in plan.block_reason


# ------------------------------------------------------- Weeks 8-9 additions
class FakeBlast:
    def __init__(self, level="CONTAINED"):
        self.level = level
        self.calls = 0

    def preview(self, plan):
        from database.blast_radius import BlastRadius

        self.calls += 1
        return BlastRadius("UPDATE", "invoice_items", direct_rows=8, table_rows=13, level=self.level)


class FakeDrift:
    def __init__(self, escalate=False):
        self.escalate = escalate

    def assess_plan(self, request, plan, turns, **_):
        from database.intent_drift import DriftAssessment

        level = "DRIFT" if self.escalate else "STABLE"
        return DriftAssessment(0.6 if self.escalate else 0.1, level, self.escalate, len(turns), reasons=["r"])


class RecordingExecutor(FakeExecutor):
    def execute(self, plan, guardrail, **kwargs):
        self.seen = (guardrail.verdict, kwargs.get("approved"))
        return super().execute(plan, guardrail, **kwargs)


def write_plan():
    return ActionPlan("write", "UPDATE invoice_items SET quantity = 1 WHERE quantity > 5", ["invoice_items"], [],
                      "x", 0.9, "SELECT COUNT(*) FROM invoice_items WHERE quantity > 5", False, None)


def committed():
    return ExecutionResult("committed", "write", "UPDATE", "ok", affected_rows=8, checks=[])


def test_wide_blast_radius_escalates_allow_to_confirm_and_does_not_execute():
    blast = FakeBlast("WIDE")
    a = agent([write_plan()], [committed()], blast_analyzer=blast)
    run = a.run("set the quantity to 1 for every line item above 5")
    assert run.status == "needs_approval" and run.stop_reason == "not_approved"
    assert a.executor.calls == 0 and blast.calls == 1
    assert run.guardrail.verdict == "ALLOW" and run.effective_verdict == "CONFIRM"
    assert [e["code"] for e in run.escalations] == ["BLAST_RADIUS_WIDE"]


def test_drift_escalation_is_reviewed_but_executor_keeps_the_week5_verdict():
    seen = {}

    def approver(plan, review):
        seen["verdict"] = review.verdict
        return True

    executor = RecordingExecutor([committed()])
    a = ReActAgent(FakeGenerator([write_plan()]), FakeGuardrails("ALLOW"), executor,
                   SessionMemory("unit-test", persist=False), approver=approver, drift_detector=FakeDrift(True))
    run = a.run("delete them")
    assert run.status == "success" and run.approved
    assert seen["verdict"] == "CONFIRM"            # the human reviews an escalated plan ...
    assert executor.seen == ("ALLOW", True)        # ... but no larger approved-CONFIRM row limit is unlocked


def test_novelty_never_relaxes_a_block():
    a = agent([write_plan()], [committed()], verdict="BLOCK", blast_analyzer=FakeBlast("CONTAINED"),
              drift_detector=FakeDrift(False))
    run = a.run("x")
    assert run.status == "blocked" and run.blast_radius is None and run.drift is None


def test_reads_skip_the_blast_radius_preview():
    blast = FakeBlast("WIDE")
    a = agent([read_plan()], [ok()], blast_analyzer=blast, drift_detector=FakeDrift(False))
    run = a.run("show x")
    assert run.status == "success" and blast.calls == 0 and run.drift.level == "STABLE"


def test_explainer_and_auditor_run_once_per_request():
    class Auditor:
        warning = None

        def __init__(self):
            self.turns = []

        def record(self, run, *, turn_no):
            self.turns.append((turn_no, run.response.headline))
            return 41 + turn_no

    from database.explainer import ResponseBuilder

    auditor = Auditor()
    a = agent([read_plan()], [ok()], explainer=ResponseBuilder(), auditor=auditor)
    first, second = a.run("show x"), a.run("show y")
    assert (first.request_id, second.request_id) == (42, 43)
    assert [t for t, _ in auditor.turns] == [1, 2]
    assert first.steps[-1].kind == "answer" and first.steps[-1].data["request_id"] == 42


def test_turn_numbers_keep_increasing_past_the_memory_cap():
    memory = SessionMemory("cap", persist=False)
    for _ in range(25):
        turn = memory.add_turn(request="r", action_type="read", status="success", final_sql=None)
    assert turn.turn_no == 25 and len(memory.turns) == 20
