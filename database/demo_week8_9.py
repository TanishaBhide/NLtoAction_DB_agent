"""End-to-end demonstration of Weeks 8 and 9 against a real PostgreSQL database.

Runs every scenario in demo_scenarios_week8_9.json through the full agent
(parser -> planner -> validator -> guardrails -> Week 9 blast-radius preview and
intent-drift check -> sandboxed executor -> ReAct loop -> Week 8 response layer
and audit). For each scenario it checks the final status AND the first root
cause, drift level, blast-radius level and predicted SQLSTATE it expects. It
then shows the audit database: v_request_audit, the stored explanations, the
drift assessments, the blast-radius previews (with whether execution confirmed
them) and one complete request trail.

    python database/demo_week8_9.py                 # reset DB, run all, offline planner
    python database/demo_week8_9.py --pause         # wait for Enter between scenarios
    python database/demo_week8_9.py --only W8-4,W9-1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

try:
    from .audit_log import STATS_QUERIES, fetch, print_trail, request_trail
    from .db import DEMO_DATABASE_URL, has_database_config
    from .demo_llm import load_scenarios
    from .demo_week6_7 import Tee
    from .display import Display
    from .react_agent import build_agent
    from .session_memory import SessionMemory
    from .setup_demo_db import apply_all
except ImportError:
    from audit_log import STATS_QUERIES, fetch, print_trail, request_trail
    from db import DEMO_DATABASE_URL, has_database_config
    from demo_llm import load_scenarios
    from demo_week6_7 import Tee
    from display import Display
    from react_agent import build_agent
    from session_memory import SessionMemory
    from setup_demo_db import apply_all

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = Path(__file__).with_name("demo_scenarios_week8_9.json")


def check(scenario: dict[str, Any], run: Any) -> tuple[bool, dict[str, Any]]:
    """Compare a final run with every expectation the scenario states."""
    why = run.response.why_not[0].code if run.response and run.response.why_not else None
    observed = {
        "status": run.status,
        "why": why,
        "drift": run.drift.level if run.drift else None,
        "blast": run.blast_radius.level if run.blast_radius else None,
        "predicted": run.blast_radius.predicted_sqlstate if run.blast_radius else None,
    }
    expected = {
        "status": scenario["expect"],
        "why": scenario.get("expect_why"),
        "drift": scenario.get("expect_drift"),
        "blast": scenario.get("expect_blast"),
        "predicted": scenario.get("expect_predicted"),
    }
    failures = {key: (expected[key], observed[key]) for key in expected
                if expected[key] is not None and expected[key] != observed[key]}
    return not failures, {"observed": observed, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="Comma-separated scenario ids, e.g. W8-4,W9-1")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset the demo database first")
    parser.add_argument("--pause", action="store_true", help="Wait for Enter between scenarios")
    parser.add_argument("--quiet-tx", action="store_true", help="Hide per-statement transaction logs")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    transcript_path = results_dir / f"week8_9_demo_{stamp}.txt"
    tee = Tee(sys.stdout, transcript_path)
    display = Display(color=not args.no_color, show_tx=not args.quiet_tx, stream=tee)

    try:
        if not args.no_reset:
            display.note("resetting demo database to the 13-row Week 1 sample (the scenarios are written for it; "
                         "python database/setup_demo_db.py restores the 2,000-row dataset) ...")
            apply_all(quiet=True, dataset="sample")

        scenarios = load_scenarios(SCENARIOS)
        if args.only:
            wanted = {item.strip().upper() for item in args.only.split(",")}
            scenarios = [scenario for scenario in scenarios if scenario["id"].upper() in wanted]

        run_tag = uuid.uuid4().hex[:8]
        sessions: list[str] = []
        state: dict[str, Any] = {"scenario": None}
        agent = build_agent(offline=True, session_id=f"demo89-{run_tag}-1", display=display,
                            interactive=False, auto_approve=False, auto_commit=True)
        sessions.append(agent.memory.session_id)

        def approver(plan: Any, guardrail: Any) -> bool:
            approve = bool(state["scenario"].get("approve"))
            display.note(f"demo reviewer {'APPROVES' if approve else 'does not approve'} this action")
            return approve

        agent.approver = approver
        display.session_header(agent.memory, offline=True)

        outcomes = []
        for index, scenario in enumerate(scenarios, 1):
            state["scenario"] = scenario
            if scenario.get("session") == "new" and index > 1:
                agent.memory = SessionMemory(f"demo89-{run_tag}-{len(sessions) + 1}")
                sessions.append(agent.memory.session_id)
            display.scenario(scenario, index, len(scenarios))
            if scenario.get("session") == "new":
                display.note(f"fresh session {agent.memory.session_id} (drift is measured within a session)")
            runs = []
            for turn in scenario.get("turns", [scenario]):
                display.user(turn["request"])
                runs.append(agent.run(turn["request"]))
            final = runs[-1]
            passed, detail = check(scenario, final)
            outcomes.append({
                "id": scenario["id"],
                "title": scenario["title"],
                "session_id": agent.memory.session_id,
                "expected": scenario["expect"],
                "passed": passed,
                **detail,
                "request_ids": [run.request_id for run in runs],
                "response": final.response.to_dict() if final.response else None,
                "drift": final.drift.to_dict() if final.drift else None,
                "blast_radius": final.blast_radius.to_dict() if final.blast_radius else None,
                "escalations": final.escalations,
            })
            if args.pause and index < len(scenarios):
                input(display.c("\n  press Enter for the next scenario ...", "grey"))

        # ------------------------------------------------------------ summary
        display.out()
        display.rule("Scenario results (status + root cause + drift + blast radius)", "bold")
        display.table(
            ["id", "scenario", "status", "first why-not", "drift", "blast", "ok"],
            [[o["id"], o["title"], o["observed"]["status"], o["observed"]["why"] or "-",
              o["observed"]["drift"] or "-", o["observed"]["blast"] or "-", "✓" if o["passed"] else "✗"]
             for o in outcomes],
            indent="  ", max_rows=50,
        )
        for o in outcomes:
            for key, (want, got) in o["failures"].items():
                display.out(display.c(f"  {o['id']}: expected {key}={want}, got {got}", "red"))
        passed = sum(o["passed"] for o in outcomes)
        style = "green" if passed == len(outcomes) else "red"
        display.out(display.c(f"\n  {passed}/{len(outcomes)} scenarios behaved as expected", style, "bold"))

        placeholders = ", ".join(["%s"] * len(sessions))
        display.out()
        display.rule("Week 8 audit database: v_request_audit for this demo", "bold")
        columns, rows = fetch(
            "SELECT request_id AS id, LEFT(request_text, 38) AS request, guardrail_verdict AS week5, "
            "effective_verdict AS effective, final_status AS status, stop_reason, attempts AS tries, "
            "sandbox_runs AS runs, COALESCE(root_cause_code, '') AS root_cause "
            f"FROM v_request_audit WHERE session_id IN ({placeholders}) ORDER BY request_id",
            tuple(sessions),
        )
        display.table(columns, rows, indent="  ", max_rows=50)

        display.out()
        display.rule("Week 9: intent drift assessments", "bold")
        columns, rows = fetch(
            "SELECT d.request_id AS id, LEFT(r.request_text, 40) AS request, d.drift_level, d.drift_score, "
            "d.baseline_turns AS baseline, d.escalated FROM drift_assessments d JOIN agent_requests r "
            f"USING (request_id) WHERE r.session_id IN ({placeholders}) ORDER BY d.request_id",
            tuple(sessions),
        )
        display.table(columns, rows, indent="  ", max_rows=50)

        display.out()
        display.rule("Week 9: blast-radius previews vs what execution did", "bold")
        columns, rows = fetch(
            "SELECT b.request_id AS id, b.statement_type AS stmt, b.target_table AS target, b.radius_level, "
            "b.direct_rows AS direct, b.cascaded_rows AS cascaded, COALESCE(LEFT(b.predicted_failure, 5), '') "
            "AS predicted, b.actual_affected_rows AS actual, COALESCE(b.actual_sqlstate, '') AS actual_state, "
            "b.prediction_correct AS correct FROM blast_radius_previews b JOIN agent_requests r USING (request_id) "
            f"WHERE r.session_id IN ({placeholders}) ORDER BY b.request_id",
            tuple(sessions),
        )
        display.table(columns, rows, indent="  ", max_rows=50)

        display.out()
        display.rule("Audit summary (python database/audit_log.py --stats)", "bold")
        for title in ("Root causes (first why-not of each request)", "Week 9: escalations by novelty module"):
            display.note(title)
            columns, rows = fetch(STATS_QUERIES[title])
            display.table(columns, rows, indent="  ", max_rows=30)
            display.out()

        featured = next((o for o in outcomes if o["id"] == "W8-4"), outcomes[0] if outcomes else None)
        if featured and featured["request_ids"][-1]:
            display.rule("One complete audit trail (python database/audit_log.py --request N)", "bold")
            print_trail(request_trail(featured["request_ids"][-1]), display)

        report_path = results_dir / f"week8_9_demo_{stamp}.json"
        report_path.write_text(
            json.dumps({"sessions": sessions, "passed": passed, "total": len(outcomes), "scenarios": outcomes},
                       indent=2, default=str),
            encoding="utf-8",
        )
        display.out()
        display.note(f"transcript : {transcript_path}")
        display.note(f"JSON report: {report_path}")
        return 0 if passed == len(outcomes) else 1
    finally:
        tee.close()


if __name__ == "__main__":
    raise SystemExit(main())
