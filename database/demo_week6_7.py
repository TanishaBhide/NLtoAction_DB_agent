"""End-to-end demonstration of Weeks 6 and 7 against a real PostgreSQL database.

Runs every scenario in demo_scenarios.json through the full pipeline:
intent parser -> planner (LLM) -> SQL validator -> Week 5 guardrails ->
Week 6 sandboxed executor -> Week 7 ReAct loop with session memory.
Then it checks that every outcome matches what was expected, shows the final
database state (proving rolled-back writes changed nothing), and lists the
audit rows and session events written to PostgreSQL.

    python database/demo_week6_7.py                 # reset DB, run all, offline planner
    python database/demo_week6_7.py --pause         # wait for Enter between scenarios
    python database/demo_week6_7.py --only W6-7,W7-1
    python database/demo_week6_7.py --live          # use the Groq/OpenAI planner instead
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

try:
    from .db import DEMO_DATABASE_URL, connect, has_database_config
    from .demo_llm import load_scenarios
    from .display import Display
    from .react_agent import build_agent
    from .setup_demo_db import apply_all
except ImportError:
    from db import DEMO_DATABASE_URL, connect, has_database_config
    from demo_llm import load_scenarios
    from display import Display
    from react_agent import build_agent
    from setup_demo_db import apply_all

ROOT = Path(__file__).resolve().parent.parent
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Tee:
    """Write to the console and (without colour codes) to a transcript file."""

    def __init__(self, console: Any, path: Path) -> None:
        self.console = console
        self.file = path.open("w", encoding="utf-8")

    def write(self, text: str) -> int:
        self.console.write(text)
        self.file.write(ANSI_RE.sub("", text))
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def query(sql: str, params: tuple = ()) -> tuple[list[str], list[list[Any]]]:
    connection = connect()
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [d[0] for d in cursor.description], [list(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="Comma-separated scenario ids, e.g. W6-2,W7-1")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset the demo database first")
    parser.add_argument("--pause", action="store_true", help="Wait for Enter between scenarios")
    parser.add_argument("--live", action="store_true", help="Use the live LLM provider instead of the scripted one")
    parser.add_argument("--quiet-tx", action="store_true", help="Hide per-statement transaction logs")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    transcript_path = results_dir / f"week6_7_demo_{stamp}.txt"
    tee = Tee(sys.stdout, transcript_path)
    display = Display(color=not args.no_color, show_tx=not args.quiet_tx, stream=tee)

    try:
        if not args.no_reset:
            display.note("resetting demo database to the Week 1 sample data ...")
            apply_all(quiet=True)

        scenarios = load_scenarios()
        if args.only:
            wanted = {item.strip().upper() for item in args.only.split(",")}
            scenarios = [scenario for scenario in scenarios if scenario["id"].upper() in wanted]

        session_id = f"demo-{uuid.uuid4().hex[:12]}"
        state: dict[str, Any] = {"scenario": None}
        agent = build_agent(
            offline=not args.live,
            session_id=session_id,
            display=display,
            interactive=False,
            auto_approve=False,
            auto_commit=True,
        )

        def approver(plan: Any, guardrail: Any) -> bool:
            approve = bool(state["scenario"].get("approve"))
            display.note(f"demo reviewer {'APPROVES' if approve else 'does not approve'} this action")
            return approve

        agent.approver = approver
        display.session_header(agent.memory, offline=not args.live)

        outcomes = []
        for index, scenario in enumerate(scenarios, 1):
            state["scenario"] = scenario
            agent.dry_run = bool(scenario.get("dry_run"))
            display.scenario(scenario, index, len(scenarios))
            if agent.dry_run:
                display.note("this scenario runs with --dry-run")
            runs = []
            for turn in scenario.get("turns", [scenario]):
                display.user(turn["request"])
                runs.append(agent.run(turn["request"]))
            final = runs[-1]
            passed = final.status == scenario["expect"]
            outcomes.append({
                "id": scenario["id"],
                "title": scenario["title"],
                "expected": scenario["expect"],
                "actual": final.status,
                "attempts": sum(run.attempts for run in runs),
                "passed": passed,
                "answer": final.answer,
                "steps": [
                    {"attempt": step.attempt, "kind": step.kind, "text": step.text}
                    for run in runs for step in run.steps
                ],
                "execution": final.execution.to_dict() if final.execution else None,
            })
            if args.pause and index < len(scenarios):
                input(display.c("\n  press Enter for the next scenario ...", "grey"))

        # ------------------------------------------------------------ summary
        display.out()
        display.rule("Scenario results", "bold")
        display.table(
            ["id", "scenario", "expected", "actual", "tries", "ok"],
            [[o["id"], o["title"], o["expected"], o["actual"], o["attempts"], "✓" if o["passed"] else "✗"]
             for o in outcomes],
            indent="  ", max_rows=50,
        )
        passed = sum(o["passed"] for o in outcomes)
        verdict_style = "green" if passed == len(outcomes) else "red"
        display.out(display.c(f"\n  {passed}/{len(outcomes)} scenarios behaved as expected", verdict_style, "bold"))

        display.out()
        display.rule("Database state after the demo (invoice 536365 and friends)", "bold")
        display.note("85123A was committed (6 -> 12); 84029G was a dry run, 71053/22752/21730 were rolled back: all unchanged")
        columns, rows = query(
            "SELECT invoice_no, stock_code, quantity, unit_price FROM invoice_items "
            "WHERE invoice_no IN ('536365', '536414') ORDER BY invoice_item_id"
        )
        display.table(columns, rows, indent="  ", max_rows=20)
        columns, rows = query(
            "SELECT stock_code, description FROM products WHERE stock_code IN ('84406B', '99999') ORDER BY 1"
        )
        display.out()
        display.note("84406B survived the failed DELETE; 99999 was inserted after approval")
        display.table(columns, rows, indent="  ")

        display.out()
        display.rule("execution_audit rows written for this session (Week 6)", "bold")
        columns, rows = query(
            "SELECT audit_id, attempt_no AS try, execution_status, guardrail_verdict AS verdict, "
            "COALESCE(sqlstate, '') AS sqlstate, affected_row_count AS rows, transaction_id AS txid, "
            "LEFT(generated_sql, 55) AS sql FROM execution_audit WHERE session_id = %s ORDER BY audit_id",
            (session_id,),
        )
        display.table(columns, rows, indent="  ", max_rows=50)
        columns, rows = query(
            "SELECT c.audit_id, c.compensation_status, c.compensation_sql FROM compensation_ledger c "
            "JOIN execution_audit a ON a.audit_id = c.audit_id WHERE a.session_id = %s ORDER BY 1",
            (session_id,),
        )
        display.out()
        display.note("compensation_ledger: undo SQL recorded for every committed write")
        display.table(columns, rows, indent="  ")

        display.out()
        display.rule("Session memory persisted in PostgreSQL (Week 7)", "bold")
        columns, rows = query(
            "SELECT event_type, COUNT(*) AS events FROM session_events WHERE session_id = %s "
            "GROUP BY event_type ORDER BY 2 DESC",
            (session_id,),
        )
        display.table(columns, rows, indent="  ")
        display.history(agent.memory)

        report_path = results_dir / f"week6_7_demo_{stamp}.json"
        report_path.write_text(
            json.dumps({"session_id": session_id, "passed": passed, "total": len(outcomes), "scenarios": outcomes},
                       indent=2, default=str),
            encoding="utf-8",
        )
        display.out()
        display.note(f"transcript : {transcript_path}")
        display.note(f"JSON report: {report_path}")
        display.note(f"resume this session interactively: python database/react_agent.py -i --offline --session-id {session_id}")
        return 0 if passed == len(outcomes) else 1
    finally:
        tee.close()


if __name__ == "__main__":
    raise SystemExit(main())
