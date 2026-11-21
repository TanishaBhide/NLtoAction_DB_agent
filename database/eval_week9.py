"""Week 9 evaluation of the two novelty modules.

1. Intent drift (offline, no database): tests/drift_sessions.json holds 40
   author-labelled sessions. The detector predicts "escalate" for the final
   request; we report precision / recall / F1 against the labels, compare with
   simple baselines (per-request guardrails alone, and two naive session rules),
   run a feature ablation and a threshold sweep.

2. Blast-radius preview (needs PostgreSQL): every write in
   tests/blast_radius_cases.json is previewed, then really executed in a
   transaction that is rolled back; full-table snapshots before/after give the
   ground-truth row changes per table and any SQLSTATE. We report exact-match
   accuracy of the predicted per-table changes, failure-prediction precision /
   recall, how often the Week 5 row count (direct rows only) underestimates the
   true change, and preview latency.

    python database/eval_week9.py              # both parts (blast part skipped without a database)
    python database/eval_week9.py --drift-only
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

warnings.filterwarnings("ignore")

try:
    from .blast_radius import BlastRadiusAnalyzer
    from .db import DEMO_DATABASE_URL, connect, has_database_config
    from .intent_drift import WEIGHTS, IntentDriftDetector
    from .setup_demo_db import apply_all
except ImportError:
    from blast_radius import BlastRadiusAnalyzer
    from db import DEMO_DATABASE_URL, connect, has_database_config
    from intent_drift import WEIGHTS, IntentDriftDetector
    from setup_demo_db import apply_all

ROOT = Path(__file__).resolve().parent.parent
DRIFT_CASES = ROOT / "tests" / "drift_sessions.json"
BLAST_CASES = ROOT / "tests" / "blast_radius_cases.json"
TABLES = ["customers", "products", "invoices", "invoice_items"]
PRIMARY_KEYS = {"customers": "customer_id", "products": "stock_code", "invoices": "invoice_no",
                "invoice_items": "invoice_item_id"}


# ------------------------------------------------------------------ drift
def _kind(sql: str) -> str:
    return sql.strip().split(None, 1)[0].lower()


def _turn(item: dict[str, Any]) -> SimpleNamespace:
    write = _kind(item["sql"]) != "select"
    return SimpleNamespace(request=item["request"], final_sql=item["sql"],
                           action_type="write" if write else "read",
                           row_count=None if write else item.get("rows"),
                           affected_rows=item.get("rows") if write else None, tables=[])


def _metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    tp = sum(1 for gold, pred in pairs if gold and pred)
    fp = sum(1 for gold, pred in pairs if not gold and pred)
    fn = sum(1 for gold, pred in pairs if gold and not pred)
    tn = sum(1 for gold, pred in pairs if not gold and not pred)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3), "accuracy": round((tp + tn) / len(pairs), 3) if pairs else 0.0}


def run_detector(detector: IntentDriftDetector, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case in sessions:
        nxt = case["next"]
        action = "read" if _kind(nxt["sql"]) == "select" else "write"
        result = detector.assess(nxt["request"], action, nxt["sql"], [_turn(t) for t in case["history"]],
                                 follow_up=bool(nxt.get("follow_up")), estimated_rows=nxt.get("rows"))
        rows.append({"id": case["id"], "gold": case["label"] == "drift", "pred": result.escalate,
                     "score": result.score, "level": result.level, "action": action,
                     "contributions": result.contributions})
    return rows


def eval_drift(path: Path = DRIFT_CASES) -> dict[str, Any]:
    sessions = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    detector = IntentDriftDetector()
    rows = run_detector(detector, sessions)
    report: dict[str, Any] = {"cases": len(rows), "detector": _metrics([(r["gold"], r["pred"]) for r in rows]),
                              "per_case": rows}

    def naive(case: dict[str, Any], rule: str) -> bool:
        nxt_kind = _kind(case["next"]["sql"])
        kinds = [_kind(t["sql"]) for t in case["history"]]
        if nxt_kind == "select":
            return False
        if rule == "write_after_reads":
            return all(k == "select" for k in kinds)
        return nxt_kind != kinds[-1]  # statement type changed from the previous turn

    report["baselines"] = {
        "week5_per_request_only": _metrics([(c["label"] == "drift", False) for c in sessions]),
        "naive_any_write_after_read_only_history": _metrics(
            [(c["label"] == "drift", naive(c, "write_after_reads")) for c in sessions]),
        "naive_statement_type_changed": _metrics(
            [(c["label"] == "drift", naive(c, "type_changed")) for c in sessions]),
    }
    ablation = {}
    for feature in WEIGHTS:
        ablated = run_detector(IntentDriftDetector(disabled_features=[feature]), sessions)
        ablation[feature] = _metrics([(r["gold"], r["pred"]) for r in ablated])
    report["ablation_without_feature"] = ablation
    sweep = {}
    for step in range(5, 13):
        threshold = round(step * 0.05, 2)
        swept = run_detector(IntentDriftDetector(drift_threshold=threshold, shift_threshold=min(0.25, threshold)),
                             sessions)
        sweep[str(threshold)] = _metrics([(r["gold"], r["pred"]) for r in swept])
    report["threshold_sweep"] = sweep
    return report


# ------------------------------------------------------------------ blast
def _snapshot(cursor: Any) -> dict[str, dict[Any, tuple]]:
    snap = {}
    for table in TABLES:
        cursor.execute(f"SELECT * FROM {table}")
        columns = [d[0] for d in cursor.description]
        key = columns.index(PRIMARY_KEYS[table])
        snap[table] = {row[key]: tuple(row) for row in cursor.fetchall()}
    return snap


def _diff(before: dict, after: dict, is_update: bool) -> dict[str, dict[str, int]]:
    changes: dict[str, dict[str, int]] = {}
    for table in TABLES:
        b, a = before[table], after[table]
        deleted = len(b.keys() - a.keys())
        inserted = len(a.keys() - b.keys())
        updated = sum(1 for k in b.keys() & a.keys() if b[k] != a[k])
        if is_update:  # a primary-key change shows up as delete + insert of the same row
            paired = min(deleted, inserted)
            deleted, inserted, updated = deleted - paired, inserted - paired, updated + paired
        slot = {name: n for name, n in (("deleted", deleted), ("inserted", inserted), ("updated", updated)) if n}
        if slot:
            changes[table] = slot
    return changes


def ground_truth(sql: str) -> tuple[dict[str, dict[str, int]], str | None]:
    connection = connect()
    try:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = 5000")
            before = _snapshot(cursor)
            try:
                cursor.execute("SAVEPOINT eval")
                cursor.execute(sql)
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            except Exception as error:  # noqa: BLE001
                cursor.execute("ROLLBACK TO SAVEPOINT eval")
                return {}, getattr(error, "pgcode", None) or type(error).__name__
            after = _snapshot(cursor)
            return _diff(before, after, sql.lstrip().upper().startswith("UPDATE")), None
    finally:
        connection.rollback()
        connection.close()


def eval_blast(path: Path = BLAST_CASES) -> dict[str, Any]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    analyzer = BlastRadiusAnalyzer()
    rows = []
    for case in cases:
        radius = analyzer.preview(case["sql"])
        actual, sqlstate = ground_truth(case["sql"])
        predicted = radius.predicted_changes() if not radius.predicted_sqlstate else {}
        in_scope = case.get("scope") != "out"
        actual_total = sum(sum(v.values()) for v in actual.values())
        rows.append({
            "id": case["id"], "note": case["note"], "in_scope": in_scope, "level": radius.level,
            "predicted_sqlstate": radius.predicted_sqlstate, "actual_sqlstate": sqlstate,
            "predicted_changes": predicted, "actual_changes": actual,
            "direct_rows": radius.direct_rows, "actual_total_rows": actual_total,
            "changes_match": predicted == actual,
            "failure_match": radius.predicted_sqlstate == sqlstate,
            "preview_ms": radius.duration_ms, "error": radius.error,
        })
    scoped = [r for r in rows if r["in_scope"]]
    succeeded = [r for r in scoped if r["actual_sqlstate"] is None]
    failure_pairs = [(r["actual_sqlstate"] is not None, r["predicted_sqlstate"] is not None) for r in scoped]
    under = [r for r in succeeded if (r["direct_rows"] or 0) < r["actual_total_rows"]]
    latencies = [r["preview_ms"] for r in rows]
    return {
        "cases": len(rows),
        "in_scope_cases": len(scoped),
        "impact_exact_match": f"{sum(r['changes_match'] for r in succeeded)}/{len(succeeded)}",
        "impact_exact_match_rate": round(sum(r["changes_match"] for r in succeeded) / len(succeeded), 3)
        if succeeded else None,
        "failure_prediction": _metrics(failure_pairs),
        "failure_sqlstate_exact": f"{sum(r['failure_match'] for r in scoped)}/{len(scoped)}",
        "week5_count_underestimates": f"{len(under)}/{len(succeeded)}",
        "rows_hidden_from_week5_count": sum(r["actual_total_rows"] - (r["direct_rows"] or 0) for r in under),
        "out_of_scope_failures": [{"id": r["id"], "actual_sqlstate": r["actual_sqlstate"], "note": r["note"]}
                                  for r in rows if not r["in_scope"]],
        "preview_ms_mean": round(statistics.mean(latencies), 2) if latencies else None,
        "preview_ms_max": max(latencies) if latencies else None,
        "per_case": rows,
    }


# ------------------------------------------------------------------- main
def _print_metrics(name: str, m: dict[str, Any]) -> None:
    print(f"  {name:<44} P {m['precision']:.2f}  R {m['recall']:.2f}  F1 {m['f1']:.2f}  "
          f"acc {m['accuracy']:.2f}   (tp {m['tp']} fp {m['fp']} fn {m['fn']} tn {m['tn']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--drift-only", action="store_true", help="Skip the PostgreSQL blast-radius part")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset the demo database first")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    report: dict[str, Any] = {"generated": datetime.now().isoformat(timespec="seconds")}
    drift = eval_drift()
    report["intent_drift"] = drift
    print(f"Intent drift detection ({drift['cases']} labelled sessions, escalate = DRIFT on a write)")
    _print_metrics("drift detector (default weights)", drift["detector"])
    for name, metrics in drift["baselines"].items():
        _print_metrics("baseline: " + name, metrics)
    print("  ablation (F1 without feature): " + ", ".join(
        f"{name} {m['f1']:.2f}" for name, m in drift["ablation_without_feature"].items()))
    print("  threshold sweep (F1):          " + ", ".join(
        f"{t} {m['f1']:.2f}" for t, m in drift["threshold_sweep"].items()))
    misses = [r["id"] + (" FN" if r["gold"] else " FP") + f" {r['score']:.2f}"
              for r in drift["per_case"] if r["gold"] != r["pred"]]
    print("  errors: " + (", ".join(misses) or "none"))

    if not args.drift_only:
        if not has_database_config():
            os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
        try:
            if not args.no_reset:
                apply_all(quiet=True, dataset="sample")
            blast = eval_blast()
        except Exception as error:  # noqa: BLE001
            print(f"\nBlast-radius evaluation skipped (PostgreSQL not reachable: {type(error).__name__}: {error})")
        else:
            report["blast_radius"] = blast
            print(f"\nBlast-radius preview ({blast['cases']} writes, {blast['in_scope_cases']} in scope; ground truth "
                  "= executed and rolled back)")
            print(f"  per-table row changes exactly predicted : {blast['impact_exact_match']} successful writes")
            fp = blast["failure_prediction"]
            print(f"  constraint-failure prediction          : P {fp['precision']:.2f} R {fp['recall']:.2f} "
                  f"(SQLSTATE exact on {blast['failure_sqlstate_exact']} in-scope cases)")
            print(f"  Week 5 count (direct rows) underestimates the true change in "
                  f"{blast['week5_count_underestimates']} successful writes, missing "
                  f"{blast['rows_hidden_from_week5_count']} row(s)")
            print("  out of scope (not predicted by design)  : " + ", ".join(
                f"{r['id']} {r['actual_sqlstate']}" for r in blast["out_of_scope_failures"]))
            print(f"  preview latency                         : mean {blast['preview_ms_mean']} ms, "
                  f"max {blast['preview_ms_max']} ms")
            for r in blast["per_case"]:
                if r["in_scope"] and not (r["changes_match"] and r["failure_match"]):
                    print(f"  mismatch {r['id']}: predicted {r['predicted_changes']} / {r['predicted_sqlstate']}, "
                          f"actual {r['actual_changes']} / {r['actual_sqlstate']}")

    out = ROOT / "results" / f"week9_eval_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
