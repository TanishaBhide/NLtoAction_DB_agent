"""Terminal rendering for the Week 6/7 agent: ReAct trace, transaction log,
pre-commit checks, result tables and captured errors."""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    from .executor import ExecutionResult
except ImportError:
    from executor import ExecutionResult

if os.name == "nt":  # enable ANSI escape processing in the Windows console
    os.system("")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "grey": "\033[90m",
}
VERDICT_COLORS = {"ALLOW": "green", "CONFIRM": "yellow", "BLOCK": "red"}
STATUS_COLORS = {
    "success": "green", "read_only": "green", "committed": "green",
    "rolled_back": "yellow", "needs_approval": "yellow",
    "failed": "red", "blocked": "red", "not_executed": "red",
}
LABELS = {
    "thought": ("Thought", "magenta"),
    "action": ("Action", "blue"),
    "observation": ("Observation", "cyan"),
    "answer": ("Answer", "bold"),
}


class Display:
    def __init__(self, *, color: bool = True, show_tx: bool = True, stream: Any = None, width: int = 100) -> None:
        self.color = color and os.environ.get("NO_COLOR") is None
        self.show_tx = show_tx
        self.stream = stream or sys.stdout
        self.width = width
        self._previewed = False

    # ------------------------------------------------------------- basics
    def c(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(CODES[style] for style in styles) + text + CODES["reset"]

    def out(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def note(self, text: str) -> None:
        self.out(self.c(f"  · {text}", "grey"))

    def rule(self, title: str = "", style: str = "bold") -> None:
        line = f"━━ {title} " if title else ""
        self.out(self.c(line + "━" * max(4, self.width - len(line)), style))

    def ask(self, question: str) -> bool:
        answer = input(self.c(f"  ? {question} [y/N] ", "yellow", "bold")).strip().lower()
        return answer in {"y", "yes"}

    def prompt(self) -> str:
        return input(self.c("\nyou> ", "bold", "green")).strip()

    # ------------------------------------------------------------ headers
    def session_header(self, memory: Any, *, offline: bool) -> None:
        llm = "scripted demo LLM (offline)" if offline else f"{os.environ.get('LLM_PROVIDER', 'groq')} (live)"
        self.rule("NL-to-Action agent · Weeks 6-7")
        self.out(f"  session : {memory.session_id}  ({len(memory.turns)} earlier turn(s) in memory)")
        self.out(f"  planner : {llm}")
        self.out("  loop    : Thought -> Action -> Observation, bounded retries, sandboxed transactions")
        if getattr(memory, "warning", None):
            self.note(memory.warning)

    def scenario(self, scenario: dict[str, Any], index: int, total: int) -> None:
        self.out()
        self.rule(f"[{index}/{total}] {scenario['id']} · {scenario['title']}", "cyan")
        self.out(self.c(f"  demonstrates: {scenario['shows']}", "grey"))

    def user(self, request: str) -> None:
        self.out()
        self.out(self.c("  USER ▶ ", "bold", "green") + self.c(request, "bold"))

    # -------------------------------------------------------------- steps
    def step(self, step: Any) -> None:
        label, style = LABELS.get(step.kind, (step.kind, "bold"))
        tag = self.c(f"  {label:<12}", style, "bold")
        if step.kind == "answer":
            status = step.data.get("status", "")
            badge = self.c(f" {status.upper()} ", STATUS_COLORS.get(status, "bold"), "bold")
            self.out(f"\n  {self.c('ANSWER', 'bold')} {badge} {step.text}")
            return
        if step.kind == "observation" and "guardrail" in step.data:
            guardrail = step.data["guardrail"]
            verdict = self.c(guardrail.verdict, VERDICT_COLORS.get(guardrail.verdict, "bold"), "bold")
            self.out(f"{tag}verdict {verdict} · risk {guardrail.risk_score:g}")
            for reason in guardrail.reasons:
                self.out(self.c(f"{'':14}- {reason}", "grey"))
            return
        self.out(f"{tag}{step.text}" if step.kind != "observation" or "execution" not in step.data else tag.rstrip())
        if step.kind == "thought" and step.data.get("memory_context"):
            self.out(self.c(f"{'':14}memory given to the planner:", "grey"))
            for line in step.data["memory_context"].splitlines():
                self.out(self.c(f"{'':16}{line}", "grey"))
        if step.kind == "action" and step.data.get("feedback"):
            self.out(self.c(f"{'':14}feedback sent to the planner:", "yellow"))
            for line in step.data["feedback"].splitlines()[1:4]:
                self.out(self.c(f"{'':16}{line}", "yellow"))
        if step.kind == "observation" and "plan" in step.data:
            plan = step.data["plan"]
            if not plan.blocked:
                self.out(self.c(f"{'':14}SQL       {plan.sql}", "cyan"))
                if plan.count_sql:
                    self.out(self.c(f"{'':14}count_sql {plan.count_sql}", "grey"))
        if step.kind == "observation" and "execution" in step.data:
            self.execution(step.data["execution"])

    # ---------------------------------------------------------- execution
    def execution(self, result: ExecutionResult) -> None:
        pad = " " * 14
        if self.show_tx and result.tx_log:
            total = sum(step.ms for step in result.tx_log)
            self.out(self.c(f"{pad}┌─ sandboxed transaction · {len(result.tx_log)} statements · {total:.1f} ms"
                            + (f" · txid {result.transaction_id}" if result.transaction_id else ""), "grey"))
            for tx in result.tx_log:
                first = tx.sql.splitlines()[0]
                if first.startswith("-- "):
                    first = first[3:]
                style = ("red",) if tx.outcome.startswith("ERROR") else (
                    ("green", "bold") if tx.sql == "COMMIT" else
                    ("yellow", "bold") if tx.sql.startswith("ROLLBACK") else
                    ("bold",) if tx.sql.startswith("BEGIN") else ("grey",))
                text = first if len(first) <= 70 else first[:67] + "..."
                self.out(self.c(f"{pad}│ ", "grey") + self.c(f"{text:<70}", *style)
                         + self.c(f" {tx.outcome:<16}{tx.ms:>7.1f} ms", "grey"))
            self.out(self.c(f"{pad}└─", "grey"))
        if result.checks:
            self.out(f"{pad}pre-commit checks:")
            for check in result.checks:
                mark = self.c("✓", "green", "bold") if check.passed else self.c("✗", "red", "bold")
                self.out(f"{pad}  {mark} {check.name:<44} " + self.c(check.detail, "grey"))
        if result.error:
            error = result.error
            self.out(self.c(f"{pad}captured error ▸ SQLSTATE {error.sqlstate} · {error.error_name} · "
                            f"{error.category} · retryable: {'yes' if error.retryable else 'no'}", "red", "bold"))
            self.out(self.c(f"{pad}  message   : {error.message}", "red"))
            for label, value in (("detail", error.detail), ("hint", error.hint),
                                 ("constraint", error.constraint), ("table", error.table)):
                if value:
                    self.out(self.c(f"{pad}  {label:<10}: {value}", "red"))
        if result.columns and result.rows and result.status in {"read_only", "committed", "rolled_back"}:
            caption = "result rows" if result.action_type == "read" else "affected rows (RETURNING *)"
            self.out(f"{pad}{caption}:")
            self.table(result.columns, result.rows, indent=pad + "  ")
        elif result.status == "read_only":
            self.out(f"{pad}(0 rows)")
        if result.compensation_sql and result.status == "committed":
            self.out(self.c(f"{pad}undo SQL (compensation_ledger):", "grey"))
            for line in result.compensation_sql[:5]:
                self.out(self.c(f"{pad}  {line}", "grey"))
        status = self.c(result.status.upper(), STATUS_COLORS.get(result.status, "bold"), "bold")
        self.out(f"{pad}status {status} · {result.reason}"
                 + (self.c(f"  [audit #{result.audit_id}]", "grey") if result.audit_id else ""))
        if result.audit_warning:
            self.note(result.audit_warning)

    def preview(self, result: ExecutionResult) -> None:
        pad = " " * 14
        self.out(self.c(f"{pad}inside the open transaction: {result.affected_rows} row(s) changed", "yellow"))
        for check in result.checks:
            mark = self.c("✓", "green") if check.passed else self.c("✗", "red")
            self.out(f"{pad}  {mark} {check.name}")

    # -------------------------------------------------------------- table
    def table(self, columns: list[str], rows: list[list[Any]], *, indent: str = "", max_rows: int = 10) -> None:
        shown = [["NULL" if value is None else str(value) for value in row] for row in rows[:max_rows]]
        widths = [min(36, max([len(str(name))] + [len(row[i]) for row in shown])) for i, name in enumerate(columns)]

        def cell(text: str, width: int) -> str:
            return (text if len(text) <= width else text[: width - 1] + "…").ljust(width)

        self.out(indent + self.c(" │ ".join(cell(str(name), widths[i]) for i, name in enumerate(columns)), "bold"))
        self.out(indent + "─┼─".join("─" * width for width in widths))
        for row in shown:
            self.out(indent + " │ ".join(cell(value, widths[i]) for i, value in enumerate(row)))
        if len(rows) > max_rows:
            self.out(self.c(f"{indent}… {len(rows) - max_rows} more row(s)", "grey"))

    # ------------------------------------------------------------ history
    def history(self, memory: Any) -> None:
        self.out()
        self.rule(f"session memory · {memory.session_id}", "magenta")
        if not memory.turns:
            self.out("  (no turns yet)")
            return
        rows = [
            [turn.turn_no, turn.request, turn.status, turn.attempts,
             turn.row_count if turn.action_type == "read" else turn.affected_rows,
             (turn.final_sql or "")]
            for turn in memory.turns
        ]
        self.table(["#", "request", "status", "tries", "rows", "final SQL"], rows, indent="  ", max_rows=50)
