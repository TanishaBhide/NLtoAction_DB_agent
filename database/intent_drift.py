"""Week 9 novelty module: session-level intent drift detection.

Per-request guardrails (Week 5) judge every statement in isolation. They
cannot see that a session which has only been *reading* invoices suddenly asks
to *delete* them ("delete those"), or that a product-browsing session jumps to
bulk-updating line items. Such turns are where fat-finger errors, ambiguous
follow-ups and injected instructions do real damage, and the statement alone
can look perfectly ordinary.

The detector builds a profile of the session from its earlier turns (what was
read or written, which tables, how many rows, which sensitive columns, what
words were used) and scores how far the new plan departs from it:

    feature                 meaning                                       weight
    action_escalation       SELECT < INSERT < UPDATE < DELETE beyond the    0.30
                            most severe statement seen so far
    follow_up_flip          a follow-up ("delete those") turns the previous 0.20
                            read into a write on the same rows
    table_novelty           the plan targets tables the session never used  0.15
    sensitivity_escalation  first write (or read) of a HIGH-sensitivity col 0.15
    scope_escalation        estimated rows vs the session's typical size    0.10
                            (log10 ratio; 100x -> 1.0)
    semantic_shift          1 - cosine(request, centroid of earlier turns)  0.10

    score = sum(weight * feature) in [0, 1]
    STABLE < 0.25 <= SHIFT < 0.45 <= DRIFT        (no earlier turn: NO_BASELINE)

Only a DRIFT on a *write* escalates the verdict (ALLOW -> CONFIRM); reads are
already sandboxed READ ONLY, so drift on a read is reported, not gated. Like
every other novelty module it can only make a decision stricter.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

try:
    from .risk_features import DEFAULT_SENSITIVE_COLUMNS
    from .sql_validator import SQLValidator
except ImportError:
    from risk_features import DEFAULT_SENSITIVE_COLUMNS
    from sql_validator import SQLValidator

WEIGHTS: dict[str, float] = {
    "action_escalation": 0.30,
    "follow_up_flip": 0.20,
    "table_novelty": 0.15,
    "sensitivity_escalation": 0.15,
    "scope_escalation": 0.10,
    "semantic_shift": 0.10,
}
SHIFT_THRESHOLD = 0.25
DRIFT_THRESHOLD = 0.45
BASELINE_TURNS = 5
STATEMENT_RANK = {"select": 0, "insert": 1, "update": 2, "delete": 3}
STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "with", "and", "or", "all", "me", "show", "list",
    "find", "get", "please", "what", "which", "is", "are", "by", "from", "that", "those", "these", "them",
    "it", "its", "their", "set", "change", "update", "delete", "remove", "add", "insert", "how", "many",
    "only", "ones", "now", "also", "then", "just", "can", "you", "i", "want", "give", "display",
}
TOKEN_RE = re.compile(r"[a-z]+")


def _a(statement: str) -> str:
    """'an UPDATE', 'a DELETE' ..."""
    word = statement.upper()
    return ("an " if word[0] in "AEIOU" else "a ") + word


@dataclass
class DriftAssessment:
    score: float
    level: str  # NO_BASELINE | STABLE | SHIFT | DRIFT
    escalate: bool
    baseline_turns: int
    features: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.level == "NO_BASELINE":
            return "NO_BASELINE: first request in this session, nothing to compare against."
        top = ", ".join(f"{name} {value:.2f}" for name, value in
                        sorted(self.contributions.items(), key=lambda item: -item[1]) if value > 0)
        return (f"{self.level} (score {self.score:.2f} vs {self.baseline_turns} earlier turn(s))"
                + (f": {top}" if top else ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TurnProfile:
    request: str
    statement: str  # select | insert | update | delete
    tables: set[str]
    sensitive_read: set[tuple[str, str]]
    sensitive_written: set[tuple[str, str]]
    rows: int | None
    tokens: Counter


def tokens(text: str) -> Counter:
    words = [w for w in TOKEN_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]
    # crude singularisation so "product" and "products" match
    return Counter(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words)


def _cosine(left: Counter, right: Counter) -> float:
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    norm = math.sqrt(sum(v * v for v in left.values())) * math.sqrt(sum(v * v for v in right.values()))
    return dot / norm if norm else 0.0


class IntentDriftDetector:
    def __init__(
        self,
        *,
        weights: dict[str, float] | None = None,
        shift_threshold: float = SHIFT_THRESHOLD,
        drift_threshold: float = DRIFT_THRESHOLD,
        baseline_turns: int = BASELINE_TURNS,
        sensitive_columns: Iterable[tuple[str, str]] = DEFAULT_SENSITIVE_COLUMNS,
        metadata_path: Path | str | None = None,
        disabled_features: Iterable[str] = (),
    ) -> None:
        self.weights = dict(weights or WEIGHTS)
        for name in disabled_features:  # used by the ablation study in eval_week9.py
            self.weights[name] = 0.0
        self.shift_threshold = shift_threshold
        self.drift_threshold = drift_threshold
        self.baseline_turns = baseline_turns
        self.sensitive = {(t.casefold(), c.casefold()) for t, c in sensitive_columns}
        path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        import json

        self.validator = SQLValidator(json.loads(path.read_text(encoding="utf-8")))

    # ------------------------------------------------------------ profiles
    def profile(self, request: str, sql: str | None, action_type: str, rows: int | None) -> TurnProfile | None:
        if not sql:
            return None
        try:
            statement = sqlglot.parse_one(sql, read="postgres")
        except Exception:  # noqa: BLE001
            return None
        if isinstance(statement, exp.Insert):
            kind = "insert"
        elif isinstance(statement, exp.Update):
            kind = "update"
        elif isinstance(statement, exp.Delete):
            kind = "delete"
        else:
            kind = "select"
        if kind == "select":
            tables = {t.name.casefold() for t in statement.find_all(exp.Table)}
        else:
            target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            tables = {target.name.casefold()} if isinstance(target, exp.Table) else set()
        sensitive_read: set[tuple[str, str]] = set()
        sensitive_written: set[tuple[str, str]] = set()
        validation = self.validator.validate(sql, "read" if kind == "select" else "write", require_limit=False)
        touched = {(t.casefold(), c.casefold()) for t, c in validation.column_references} & self.sensitive
        if kind == "select":
            sensitive_read = touched
        else:
            sensitive_written = {(t, c) for t, c in touched if t in tables}
            sensitive_read = touched - sensitive_written
        return TurnProfile(request, kind, tables, sensitive_read, sensitive_written, rows, tokens(request))

    def _baseline(self, turns: Iterable[Any]) -> list[TurnProfile]:
        profiles = []
        for turn in turns:
            sql = getattr(turn, "final_sql", None)
            action = getattr(turn, "action_type", "read")
            rows = getattr(turn, "affected_rows", None) if action == "write" else getattr(turn, "row_count", None)
            profile = self.profile(getattr(turn, "request", ""), sql, action, rows)
            if profile is not None:
                profiles.append(profile)
        return profiles[-self.baseline_turns:]

    # -------------------------------------------------------------- assess
    def assess(
        self,
        request: str,
        action_type: str,
        sql: str | None,
        history: Iterable[Any],
        *,
        follow_up: bool = False,
        estimated_rows: int | None = None,
    ) -> DriftAssessment:
        baseline = self._baseline(history)
        new = self.profile(request, sql, action_type, estimated_rows)
        if not baseline or new is None:
            return DriftAssessment(0.0, "NO_BASELINE", False, len(baseline))

        features: dict[str, float] = {}
        reasons: list[str] = []

        top_rank = max(STATEMENT_RANK[p.statement] for p in baseline)
        new_rank = STATEMENT_RANK[new.statement]
        features["action_escalation"] = max(0, new_rank - top_rank) / 3
        if features["action_escalation"]:
            seen = max(baseline, key=lambda p: STATEMENT_RANK[p.statement]).statement.upper()
            reasons.append(f"This is {_a(new.statement)}, but the most severe statement in the last "
                           f"{len(baseline)} turn(s) was {_a(seen)}.")

        previous = baseline[-1]
        flip = 0.0
        if follow_up and new.statement != previous.statement:
            if previous.statement == "select" and new.statement != "select":
                flip = 1.0
            elif STATEMENT_RANK[new.statement] > STATEMENT_RANK[previous.statement]:
                flip = 0.5
        features["follow_up_flip"] = flip
        if flip:
            reasons.append(f"The request is a follow-up of \"{previous.request}\" ({previous.statement.upper()}) "
                           f"but turns it into {_a(new.statement)} on the same rows.")

        seen_tables = set().union(*(p.tables for p in baseline))
        novel = new.tables - seen_tables
        features["table_novelty"] = len(novel) / len(new.tables) if new.tables else 0.0
        if novel:
            reasons.append(f"It targets table(s) this session has not used yet: {', '.join(sorted(novel))}.")

        written_before = set().union(*(p.sensitive_written for p in baseline))
        touched_before = written_before | set().union(*(p.sensitive_read for p in baseline))
        sens = 0.0
        if new.sensitive_written - written_before:
            sens = 1.0
            cols = ", ".join(f"{t}.{c}" for t, c in sorted(new.sensitive_written - written_before))
            reasons.append(f"It is the session's first write involving HIGH-sensitivity column(s) {cols}.")
        elif new.sensitive_read - touched_before:
            sens = 0.5
            cols = ", ".join(f"{t}.{c}" for t, c in sorted(new.sensitive_read - touched_before))
            reasons.append(f"It is the session's first access to HIGH-sensitivity column(s) {cols}.")
        features["sensitivity_escalation"] = sens

        sizes = [p.rows for p in baseline if p.rows is not None]
        scope = 0.0
        if new.rows is not None and sizes:
            typical = max(1.0, statistics.median(sizes))
            ratio = new.rows / typical
            if ratio > 1:
                scope = min(1.0, math.log10(ratio) / 2)
            if scope >= 0.15:
                reasons.append(f"It would touch about {new.rows} row(s), {ratio:.0f}x the session's typical "
                               f"{typical:g}.")
        features["scope_escalation"] = round(scope, 4)

        centroid: Counter = Counter()
        for p in baseline:
            centroid.update(p.tokens)
        similarity = _cosine(new.tokens, centroid)
        features["semantic_shift"] = round(1 - similarity, 4) if new.tokens and centroid else 0.0
        if features["semantic_shift"] >= 0.8 and not novel:
            reasons.append("The wording shares almost nothing with the earlier requests.")

        contributions = {name: round(self.weights.get(name, 0.0) * value, 4) for name, value in features.items()}
        score = round(min(1.0, sum(contributions.values())), 4)
        level = "DRIFT" if score >= self.drift_threshold else "SHIFT" if score >= self.shift_threshold else "STABLE"
        escalate = level == "DRIFT" and new.statement != "select"
        return DriftAssessment(score, level, escalate, len(baseline), features, contributions, reasons)

    def assess_plan(self, request: str, plan: Any, turns: Iterable[Any], *, parsed: Any = None,
                    estimated_rows: int | None = None) -> DriftAssessment:
        return self.assess(
            request,
            str(getattr(plan, "action_type", "read")),
            getattr(plan, "sql", None),
            turns,
            follow_up=bool(getattr(parsed, "resolved_from_context", False)),
            estimated_rows=estimated_rows,
        )
