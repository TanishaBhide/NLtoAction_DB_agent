"""Week 7: end-to-end agentic action loop (ReAct: reason -> act -> observe).

For each natural-language request the agent repeats, up to ``max_attempts``:

    Thought      decide the next step from the latest observation
    Action       generate_sql        (Week 4 planner, with memory + feedback)
    Observation  validated plan, or a block
    Action       check_guardrails    (Week 5 rules + risk classifier)
    Observation  ALLOW / CONFIRM / BLOCK
    Action       execute_sandboxed   (Week 6 transaction + pre-commit checks)
    Observation  rows, affected rows, checks, or a captured SQL error

Retry policy (bounded):
  * retryable SQL errors (SQLSTATE classes 42, 22, 40, 55) are fed back to the
    planner as the next attempt's EXECUTION FEEDBACK;
  * a read that succeeds with 0 rows gets ONE result-feedback retry;
  * constraint violations (23), trigger rejections (P0), pre-commit check
    failures and guardrail BLOCKs are final: retrying would mean trying to get
    around a safety rule, so the agent stops and explains instead.

Every step is printed live, appended to the run trace, and stored in
session_events; every finished turn is added to session memory.

    python database/react_agent.py "show customers from France"
    python database/react_agent.py --interactive
    python database/react_agent.py --offline --interactive   # scripted demo LLM
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from .db import DEMO_DATABASE_URL, has_database_config
    from .executor import ExecutionResult, SafeExecutor
    from .guardrails import GuardrailEngine, GuardrailResult
    from .session_memory import SessionMemory
    from .sql_generator import ActionPlan, SQLGenerator
except ImportError:
    from db import DEMO_DATABASE_URL, has_database_config
    from executor import ExecutionResult, SafeExecutor
    from guardrails import GuardrailEngine, GuardrailResult
    from session_memory import SessionMemory
    from sql_generator import ActionPlan, SQLGenerator

DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class Step:
    index: int
    attempt: int
    kind: str  # thought | action | observation | answer
    text: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRun:
    request: str
    session_id: str
    status: str = "failed"  # success | blocked | needs_approval | rolled_back | failed
    answer: str = ""
    attempts: int = 0
    steps: list[Step] = field(default_factory=list)
    plan: ActionPlan | None = None
    guardrail: GuardrailResult | None = None
    execution: ExecutionResult | None = None


class ReActAgent:
    def __init__(
        self,
        generator: SQLGenerator,
        guardrails: GuardrailEngine,
        executor: SafeExecutor,
        memory: SessionMemory,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        approver: Callable[[ActionPlan, GuardrailResult], bool] | None = None,
        commit_decider: Callable[[ExecutionResult], bool] | None = None,
        dry_run: bool = False,
        on_step: Callable[[Step], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        self.generator = generator
        self.guardrails = guardrails
        self.executor = executor
        self.memory = memory
        self.max_attempts = max_attempts
        self.approver = approver
        self.commit_decider = commit_decider
        self.dry_run = dry_run
        self.on_step = on_step

    # ----------------------------------------------------------------- trace
    def _emit(self, run: AgentRun, attempt: int, kind: str, text: str, **data: Any) -> None:
        step = Step(len(run.steps) + 1, attempt, kind, text, data)
        run.steps.append(step)
        if self.on_step:
            self.on_step(step)
        payload: dict[str, Any] = {"attempt": attempt, "text": text, "request": run.request}
        for key, value in data.items():
            if isinstance(value, ExecutionResult):
                payload[key] = {
                    "status": value.status,
                    "row_count": value.row_count,
                    "affected_rows": value.affected_rows,
                    "error": value.error.feedback() if value.error else None,
                    "checks": [(check.name, check.passed) for check in value.checks],
                }
            elif isinstance(value, (ActionPlan, GuardrailResult)):
                payload[key] = {k: v for k, v in vars(value).items() if k != "classifier_probabilities"}
            else:
                payload[key] = value
        self.memory.log_event(kind, payload)

    # ------------------------------------------------------------- feedback
    @staticmethod
    def _error_feedback(attempt: int, plan: ActionPlan, result: ExecutionResult) -> str:
        assert result.error is not None
        return (
            f"Previous attempts: {attempt}\n"
            f"The previous SQL passed validation but FAILED when executed in a rolled-back sandbox.\n"
            f"Failed SQL: {plan.sql}\n"
            f"{result.error.feedback()}\n"
            f"Meaning: {result.error.explanation}\n"
            "Return a corrected plan for the same request. Respect column data types "
            "(identifier columns such as customer_id and invoice_no are VARCHAR and need quoted literals)."
        )

    @staticmethod
    def _empty_feedback(attempt: int, plan: ActionPlan) -> str:
        return (
            f"Previous attempts: {attempt}\n"
            f"The previous SQL executed successfully but returned 0 rows.\n"
            f"SQL: {plan.sql}\n"
            "Check whether a filter is too strict: text in this database is often upper case "
            "(prefer ILIKE for text search), identifiers are strings, and dates are TIMESTAMPs. "
            "If 0 rows is genuinely the right answer, return the same SQL."
        )

    # ------------------------------------------------------------------ loop
    def run(self, request: str) -> AgentRun:
        run = AgentRun(request=request, session_id=self.memory.session_id)
        memory_context = self.memory.context_text()
        feedback: str | None = None
        empty_retry_used = False
        previous_sql: str | None = None

        earlier = len(self.memory.turns)
        self._emit(
            run, 1, "thought",
            "New request. "
            + (f"Session memory holds {earlier} earlier turn(s); I pass the last few to the planner as context. "
               if earlier else "No earlier turns in this session. ")
            + "First I need a validated SQL plan.",
            memory_context=memory_context,
        )

        for attempt in range(1, self.max_attempts + 1):
            run.attempts = attempt
            # ---- Act: plan
            self._emit(
                run, attempt, "action",
                f"generate_sql(attempt {attempt}/{self.max_attempts}"
                + (", with execution feedback)" if feedback else ")"),
                feedback=feedback,
            )
            plan = self.generator.generate(
                request, session_id=self.memory.session_id, feedback=feedback, memory_context=memory_context
            )
            run.plan = plan
            parsed = getattr(self.generator, "last_parsed", None)
            if attempt == 1 and parsed is not None and parsed.resolved_from_context:
                self._emit(run, attempt, "observation",
                           f"Intent parser resolved this as a follow-up of \"{parsed.previous_request}\" "
                           "(session memory); inherited constraints: "
                           + ", ".join(f"{c.get('column')} {c.get('operator')} {c.get('value')!r}"
                                       for c in parsed.constraints))
            if plan.blocked:
                self._emit(run, attempt, "observation", f"Plan blocked: {plan.block_reason}", plan=plan)
                self._emit(run, attempt, "thought",
                           "The planner refused this request. I will not retry around a block; stopping.")
                run.status, run.answer = "blocked", f"Request blocked before execution: {plan.block_reason}"
                break
            self._emit(run, attempt, "observation",
                       f"{plan.action_type.upper()} plan (confidence {plan.confidence:.2f}): {plan.explanation}",
                       plan=plan)
            if previous_sql is not None and plan.sql.strip() == previous_sql.strip() and feedback:
                if run.execution and run.execution.status == "read_only":
                    self._emit(run, attempt, "thought",
                               "The planner returned the same SQL, confirming the empty result is correct.")
                    run.status = "success"
                    run.answer = "The query ran successfully; no rows match the request."
                    break

            # ---- Act: guardrails
            self._emit(run, attempt, "action", "check_guardrails(plan)")
            guardrail = self.guardrails.evaluate(plan, context={"session_id": self.memory.session_id})
            run.guardrail = guardrail
            self._emit(run, attempt, "observation",
                       f"Verdict {guardrail.verdict} (risk {guardrail.risk_score:g}): " + "; ".join(guardrail.reasons),
                       guardrail=guardrail)
            if guardrail.verdict == "BLOCK":
                self._emit(run, attempt, "thought", "Guardrails blocked the plan. Safety blocks are final; stopping.")
                run.status, run.answer = "blocked", "Blocked by guardrails: " + "; ".join(guardrail.reasons)
                break
            approved = False
            if guardrail.verdict == "CONFIRM":
                self._emit(run, attempt, "thought", "The verdict is CONFIRM, so a human must approve before execution.")
                self._emit(run, attempt, "action", "request_human_approval(plan)")
                approved = bool(self.approver(plan, guardrail)) if self.approver else False
                self._emit(run, attempt, "observation", "Approved by the user." if approved else "Not approved.",
                           approved=approved)
                if not approved:
                    run.status = "needs_approval"
                    run.answer = "Not executed: this action needs human approval (Week 5 verdict CONFIRM)."
                    break

            # ---- Act: execute in the sandbox
            sandbox = ("a READ ONLY transaction" if plan.action_type == "read"
                       else "a READ WRITE transaction with pre-commit row and constraint checks")
            self._emit(run, attempt, "thought", f"The plan is cleared. I will run it inside {sandbox}.")
            self._emit(run, attempt, "action", "execute_sandboxed(plan)" + (" [dry run]" if self.dry_run else ""))
            result = self.executor.execute(
                plan, guardrail,
                approved=approved,
                dry_run=self.dry_run,
                commit_decider=self.commit_decider,
                session_id=self.memory.session_id,
                request_text=request,
                attempt_no=attempt,
            )
            run.execution = result
            self._emit(run, attempt, "observation", f"{result.status}: {result.reason}", execution=result)
            previous_sql = plan.sql

            # ---- Reason about the observation
            if result.status == "failed" and result.error is not None:
                if result.error.retryable and attempt < self.max_attempts:
                    self._emit(run, attempt, "thought",
                               f"SQLSTATE {result.error.sqlstate} is a {result.error.category} error, which a "
                               "corrected query can fix. I will feed the error back to the planner and retry.")
                    feedback = self._error_feedback(attempt, plan, result)
                    continue
                why = ("the retry budget is exhausted" if result.error.retryable
                       else f"a {result.error.category} error means the request itself breaks a rule")
                self._emit(run, attempt, "thought", f"Stopping: {why}. The transaction was rolled back.")
                run.status = "failed"
                run.answer = (f"Could not complete the request: {result.error.message}. {result.error.explanation} "
                              "Nothing was changed.")
                break
            if result.status == "not_executed":
                run.status, run.answer = "blocked", result.reason
                break
            if result.status == "rolled_back":
                failed = [check for check in result.checks if not check.passed]
                if failed:
                    self._emit(run, attempt, "thought",
                               "The SQL ran, but a pre-commit check failed, so it was rolled back. The plan does "
                               "what was asked; the request breaks a data rule. Not retrying.")
                    run.status = "rolled_back"
                    run.answer = ("Rolled back. Pre-commit check failed: "
                                  + "; ".join(f"{check.name} ({check.detail})" for check in failed))
                else:
                    run.status = "rolled_back"
                    run.answer = f"Rolled back, nothing changed: {result.reason}"
                break
            if (result.status == "read_only" and result.row_count == 0
                    and not empty_retry_used and attempt < self.max_attempts):
                empty_retry_used = True
                self._emit(run, attempt, "thought",
                           "The query worked but returned 0 rows. A filter may be too strict, so I will "
                           "feed this result back for one retry.")
                feedback = self._empty_feedback(attempt, plan)
                continue

            run.status = "success"
            if result.status == "read_only":
                run.answer = (f"Returned {result.row_count} row(s)"
                              + (" (truncated)" if result.truncated else "")
                              + f" after {attempt} attempt(s).")
            else:
                verb = plan.sql.split(None, 1)[0].upper()
                run.answer = (f"{verb} committed: {result.affected_rows} row(s) changed in transaction "
                              f"{result.transaction_id}. All {len(result.checks)} pre-commit checks passed.")
                if result.compensation_sql:
                    run.answer += " Undo SQL was stored in compensation_ledger."
            break
        else:  # pragma: no cover - loop always breaks or continues to exhaustion
            run.status = "failed"

        if run.status == "failed" and not run.answer:
            run.answer = "Retry budget exhausted without a successful execution."
        self._emit(run, run.attempts, "answer", run.answer, status=run.status)
        execution = run.execution
        self.memory.add_turn(
            request=request,
            action_type=(run.plan.action_type if run.plan else "unknown"),
            status=run.status,
            final_sql=(run.plan.sql if run.plan and not run.plan.blocked else None),
            tables=(run.plan.tables if run.plan else []),
            row_count=(execution.row_count if execution else None),
            affected_rows=(execution.affected_rows if execution else None),
            attempts=run.attempts,
            error=(execution.error.message if execution and execution.error else None),
            result_preview=(execution.rows[:3] if execution and execution.status == "read_only" else []),
        )
        return run


# --------------------------------------------------------------------- CLI
def build_agent(
    *,
    offline: bool,
    session_id: str | None,
    display: Any,
    interactive: bool,
    auto_approve: bool = False,
    auto_commit: bool = True,
    dry_run: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    scenarios_path: str | None = None,
) -> ReActAgent:
    llm_client = None
    if offline:
        try:
            from .demo_llm import ScriptedLLMClient
        except ImportError:
            from demo_llm import ScriptedLLMClient
        llm_client = ScriptedLLMClient(scenarios_path)

    def approver(plan: ActionPlan, guardrail: GuardrailResult) -> bool:
        if auto_approve:
            display.note("auto-approved (--approve)")
            return True
        if not interactive:
            return False
        return display.ask(f"Guardrails require approval (risk {guardrail.risk_score:g}). Approve this action?")

    def commit_decider(result: ExecutionResult) -> bool:
        if not interactive:
            return auto_commit
        display.preview(result)
        return display.ask(f"All checks passed. COMMIT {result.affected_rows} row change(s)?")

    return ReActAgent(
        SQLGenerator(llm_client=llm_client),
        GuardrailEngine(),
        SafeExecutor(),
        SessionMemory(session_id),
        max_attempts=max_attempts,
        approver=approver,
        commit_decider=commit_decider,
        dry_run=dry_run,
        on_step=display.step,
    )


def main(argv: list[str] | None = None) -> int:
    warnings.filterwarnings("ignore")
    try:
        from .display import Display
    except ImportError:
        from display import Display

    parser = argparse.ArgumentParser(description="Week 7 ReAct NL-to-Action agent (executes against PostgreSQL).")
    parser.add_argument("request", nargs="?", help="Natural-language request")
    parser.add_argument("--interactive", "-i", action="store_true", help="Session REPL (commands: history, exit)")
    parser.add_argument("--session-id", default=None, help="Resume or name a session (memory persists in PostgreSQL)")
    parser.add_argument("--offline", action="store_true", help="Use the scripted demo LLM instead of Groq/OpenAI")
    parser.add_argument("--approve", action="store_true", help="Auto-approve CONFIRM verdicts (non-interactive)")
    parser.add_argument("--no-commit", action="store_true", help="Roll back writes even when checks pass")
    parser.add_argument("--dry-run", action="store_true", help="Execute writes and always roll back")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--quiet-tx", action="store_true", help="Hide the per-statement transaction log")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    if not args.interactive and not args.request:
        parser.error("a request is required unless --interactive is used")

    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    display = Display(color=not args.no_color, show_tx=not args.quiet_tx)
    if not args.offline and not (os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        display.note("No GROQ_API_KEY/OPENAI_API_KEY found: switching to the offline scripted demo LLM.")
        args.offline = True

    agent = build_agent(
        offline=args.offline,
        session_id=args.session_id,
        display=display,
        interactive=args.interactive,
        auto_approve=args.approve,
        auto_commit=not args.no_commit,
        dry_run=args.dry_run,
        max_attempts=args.max_attempts,
    )
    display.session_header(agent.memory, offline=args.offline)

    if not args.interactive:
        display.user(args.request)
        run = agent.run(args.request)
        return 0 if run.status == "success" else 1

    while True:
        try:
            text = display.prompt()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            break
        if text.lower() in {"history", ":history"}:
            display.history(agent.memory)
            continue
        agent.run(text)
    display.history(agent.memory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
