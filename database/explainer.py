"""Week 8: plain-English responses and "why not" root-cause explanations.

The ReAct loop (Week 7) ends with a terse technical answer. This module turns
the whole run (plan, guardrail verdict, novelty escalations, every sandboxed
attempt and its captured error or failed check) into:

  * a one-line headline and a short plain-English summary of what happened;
  * for anything that did not succeed, an ordered list of root-cause
    explanations ("why not"): which pipeline stage stopped the request, the
    concrete cause with evidence (rule, constraint, SQLSTATE, invariant counts),
    and what the user could do instead;
  * notes about retries, the blast-radius preview, intent drift and undo SQL.

It is deterministic and template-based on purpose: every sentence is built
from facts the pipeline recorded, so the explanation cannot invent a cause
(an LLM paraphrase could). Explanations are stored per request in
request_explanations by audit_log.py.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

try:
    from .executor import INVARIANTS
    from .guardrails import BLOCK_ROW_THRESHOLD, CONFIRM_ROW_THRESHOLD
except ImportError:
    from executor import INVARIANTS
    from guardrails import BLOCK_ROW_THRESHOLD, CONFIRM_ROW_THRESHOLD

NOUNS = {
    "customers": ("customer", "customers"),
    "products": ("product", "products"),
    "invoices": ("invoice", "invoices"),
    "invoice_items": ("line item", "line items"),
}

CONSTRAINT_TEXT = {
    "chk_invoice_items_quantity": "a line item's quantity can never be 0",
    "chk_invoices_status": "an invoice's status must be completed, cancelled or adjustment",
    "fk_invoice_items_product": "every line item must point at an existing product, and a product that is "
                                "still on a line item cannot be deleted: ON DELETE RESTRICT",
    "fk_invoice_items_invoice": "every line item must belong to an existing invoice",
    "fk_invoices_customer": "an invoice's customer must exist in customers",
    "customers_pkey": "customer IDs are unique",
    "products_pkey": "stock codes are unique",
    "invoices_pkey": "invoice numbers are unique",
    "invoice_items_pkey": "line-item IDs are unique",
}

TRIGGER_TEXT = {
    "negative unit_price is only allowed for adjustment invoices":
        "the trigger trg_invoice_items_validate_unit_price only allows a negative unit price on adjustment "
        "invoices (invoice numbers starting with A)",
}

RULE_FIX = {
    "WHERE_REQUIRED": "Name the rows to change, for example \"... for invoice 536365\".",
    "ROW_LIMIT": (f"Narrow the filter so it matches at most {CONFIRM_ROW_THRESHOLD} rows "
                  f"({BLOCK_ROW_THRESHOLD} with a reviewer's approval)."),
    "PII_COLUMN": "Ask for aggregates (for example a count per country) instead of raw customer IDs, or add a "
                  "specific filter and a LIMIT; otherwise a reviewer has to approve it.",
    "DDL_BLOCK": "Schema changes go through a reviewed migration by a DBA, not through the agent.",
    "MULTI_STATEMENT": "Ask for one action per request, without SQL comments.",
    "WRITE_TAUTOLOGY": "Filter on a real column value; a condition like 1 = 1 matches every row.",
}

RULE_HEADLINE = {
    "WHERE_REQUIRED": "The change has no filter, so it would hit every row",
    "ROW_LIMIT": "Too many rows would be affected",
    "PII_COLUMN": "The query exposes or changes customer identifiers",
    "DDL_BLOCK": "Schema changes (DDL) are never run by the agent",
    "MULTI_STATEMENT": "Only one plain statement per request is allowed",
    "WRITE_TAUTOLOGY": "The filter is always true, so it would hit every row",
}

PLANNER_BLOCKS = [
    ("DDL is blocked", "PLANNER_DDL", "Schema changes (DDL) are never run by the agent",
     "Dropping, altering or truncating tables is outside what the agent may do.",
     RULE_FIX["DDL_BLOCK"]),
    ("Unscoped writes are blocked", "PLANNER_UNSCOPED_WRITE", "The change has no filter",
     "The request would change or delete rows without saying which ones, so it could affect the whole table.",
     RULE_FIX["WHERE_REQUIRED"]),
    ("could not classify", "PLANNER_UNCLASSIFIED", "I could not tell what you want to do",
     "The intent parser could not classify the request as a read or a write.",
     "Rephrase it starting with a verb such as show, count, set, add or delete."),
    ("LLM unavailable", "PLANNER_LLM_UNAVAILABLE", "The SQL planner (LLM) was not reachable",
     "No plan could be generated, so nothing was run.",
     "Check GROQ_API_KEY / network access and try again."),
    ("Offline demo mode", "PLANNER_OFFLINE_UNKNOWN", "The offline demo planner does not know this request",
     "Without an API key the agent only answers the scripted demo requests.",
     "Set GROQ_API_KEY to ask anything."),
    ("Invalid generated action plan", "PLANNER_INVALID_PLAN", "The generated SQL failed validation",
     "The planner's SQL did not pass schema validation, even after one correction attempt.",
     "Rephrase the request using table and column names from the schema."),
]


@dataclass
class Explanation:
    stage: str  # planning | guardrails | drift | blast_radius | approval | execution | precommit | retry | result
    code: str
    headline: str
    cause: str
    fix: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return f"{self.headline}. {self.cause}" + (f" What you can do: {self.fix}" if self.fix else "")


@dataclass
class AgentResponse:
    status: str
    headline: str
    summary: str
    why_not: list[Explanation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [self.headline]
        if self.summary:
            lines.append(self.summary)
        if self.why_not:
            lines.append("Why not:")
            lines += [f"  {i}. [{e.stage}] {e.text()}" for i, e in enumerate(self.why_not, 1)]
        if self.notes:
            lines.append("Notes:")
            lines += [f"  - {note}" for note in self.notes]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ helpers
def _lower_first(text: str) -> str:
    """Lower-case the first word unless it is an acronym such as DDL or SQL."""
    first = text.split(" ", 1)[0]
    return text if first.isupper() or (len(first) > 1 and first[1].isupper()) else text[:1].lower() + text[1:]


def _noun(table: str | None, count: int) -> str:
    singular, plural = NOUNS.get(table or "", ("row", "rows"))
    return singular if count == 1 else plural


def _parse(sql: str | None) -> exp.Expression | None:
    if not sql:
        return None
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001
        return None


def _target(statement: exp.Expression | None) -> str | None:
    if statement is None:
        return None
    if isinstance(statement, (exp.Insert, exp.Update, exp.Delete)):
        node = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        return node.name if isinstance(node, exp.Table) else None
    source = statement.args.get("from") or statement.args.get("from_")
    if source is not None and isinstance(source.this, exp.Table):
        return source.this.name
    table = statement.find(exp.Table)
    return table.name if table else None


def _where_text(statement: exp.Expression | None) -> str | None:
    where = statement.args.get("where") if statement is not None else None
    return where.this.sql(dialect="postgres") if where is not None else None


def _fmt(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe_rows(table: str | None, columns: list[str], rows: list[list[Any]], row_count: int,
                  truncated: bool = False, group_by: list[str] | None = None) -> tuple[str, str]:
    """Headline and summary for a read result."""
    if row_count == 0:
        return f"No {_noun(table, 2)} match your request.", ""
    if row_count == 1 and len(columns) == 1:
        return f"The answer is {_fmt(rows[0][0])}.", f"({columns[0]} = {_fmt(rows[0][0])})"
    more = " (only the first 100 are shown)" if truncated else ""
    if group_by and row_count == 1:
        values = ", ".join(f"{c} {_fmt(v)}" for c, v in zip(columns[len(group_by):], rows[0][len(group_by):]))
        key = ", ".join(_fmt(v) for v in rows[0][:len(group_by)])
        return f"Result: {key}" + (f" ({values})." if values else "."), ""
    if group_by:
        headline = (f"Here are {row_count}{'+' if truncated else ''} results, one per {', '.join(group_by)}"
                    f"{more}.")
    else:
        headline = f"Found {row_count}{'+' if truncated else ''} {_noun(table, row_count)}{more}."
    if len(columns) == 2 and row_count <= 8:
        pairs = "; ".join(f"{_fmt(r[0])}: {_fmt(r[1])}" for r in rows)
        return headline, f"{columns[0]} → {columns[1]}: {pairs}."
    shown = rows[:3]
    listing = "; ".join(", ".join(f"{c}={_fmt(v)}" for c, v in zip(columns, r)) for r in shown)
    rest = f" … and {row_count - len(shown)} more" if row_count > len(shown) else ""
    return headline, f"{'First rows' if row_count > 1 else 'Row'}: {listing}{rest}."


def describe_write(sql: str, affected: int | None) -> str:
    statement = _parse(sql)
    table = _target(statement)
    n = affected or 0
    where = _where_text(statement)
    scope = f" where {where}" if where else ""
    if isinstance(statement, exp.Update):
        sets = ", ".join(a.sql(dialect="postgres") for a in statement.expressions)
        return f"set {sets} on {n} {_noun(table, n)} in {table}{scope}"
    if isinstance(statement, exp.Delete):
        return f"deleted {n} {_noun(table, n)} from {table}{scope}"
    if isinstance(statement, exp.Insert):
        return f"added {n} {_noun(table, n)} to {table}"
    return f"changed {n} row(s)"


# --------------------------------------------------------------- explainer
class ResponseBuilder:
    def build(self, run: Any) -> AgentResponse:
        reason = getattr(run, "stop_reason", "") or ""
        handler = getattr(self, f"_on_{reason}", None)
        response = handler(run) if handler else self._fallback(run)
        response.status = run.status
        response.notes = self._notes(run, response.notes)
        return response

    # ----------------------------------------------------------- success
    def _on_answered(self, run: Any) -> AgentResponse:
        result = run.execution
        statement = _parse(run.plan.sql if run.plan else None)
        group = statement.args.get("group") if statement is not None else None
        group_by = ([node.name if isinstance(node, exp.Column) else node.sql(dialect="postgres")
                     for node in group.expressions] if group is not None else None)
        headline, summary = describe_rows(_target(statement), result.columns, result.rows, result.row_count,
                                          result.truncated, group_by)
        return AgentResponse(run.status, headline, summary)

    def _on_empty_confirmed(self, run: Any) -> AgentResponse:
        statement = _parse(run.plan.sql if run.plan else None)
        table = _target(statement)
        where = _where_text(statement)
        headline = f"No {_noun(table, 2)} match your request."
        summary = "The query ran correctly and was double-checked after an empty first result."
        why = [Explanation(
            "result", "EMPTY_RESULT", "Nothing matched the filter",
            f"No row in {table} satisfies {where}." if where else f"{table} returned no rows.",
            "Check the spelling or value of the filter, or widen it.",
            {"where": where},
        )]
        return AgentResponse(run.status, headline, summary, why)

    def _on_committed(self, run: Any) -> AgentResponse:
        result = run.execution
        action = describe_write(run.plan.sql, result.affected_rows)
        headline = f"Done: {action.split(' where ')[0]}."
        summary = (f"I {action}. The change was committed in transaction {result.transaction_id} after all "
                   f"{len(result.checks)} pre-commit checks passed.")
        return AgentResponse(run.status, headline, summary)

    # ------------------------------------------------------- not success
    def _on_planner_block(self, run: Any) -> AgentResponse:
        reason = (run.plan.block_reason if run.plan else None) or run.answer
        for marker, code, headline, cause, fix in PLANNER_BLOCKS:
            if marker.lower() in reason.lower():
                why = [Explanation("planning", code, headline, cause, fix, {"block_reason": reason})]
                break
        else:
            why = [Explanation("planning", "PLANNER_MODEL_REFUSED", "The planner refused this request",
                               reason, "Rephrase the request as one specific read or change.",
                               {"block_reason": reason})]
        return AgentResponse(run.status, f"I did not run this: {_lower_first(why[0].headline)}.",
                             "Nothing was sent to the database.", why)

    def _on_guardrail_block(self, run: Any) -> AgentResponse:
        guardrail = run.guardrail
        why = self._guardrail_reasons(guardrail)
        return AgentResponse(run.status, f"I did not run this: {_lower_first(why[0].headline)}.",
                             f"Week 5 guardrails returned BLOCK (risk {guardrail.risk_score:g}/100). "
                             "Blocks are final: the agent never retries around them. Nothing was sent to the "
                             "database.", why)

    def _on_not_approved(self, run: Any) -> AgentResponse:
        why = []
        guardrail = run.guardrail
        if guardrail is not None and guardrail.verdict == "CONFIRM":
            why += self._guardrail_reasons(guardrail)
        for item in getattr(run, "escalations", []) or []:
            why.append(Explanation(item["source"], item["code"], item["headline"], item["reason"],
                                   item.get("fix"), item.get("evidence", {})))
        why.append(Explanation("approval", "APPROVAL_NOT_GIVEN", "Nobody approved it",
                               "A CONFIRM verdict is only executed after a human says yes.",
                               "Re-run interactively (or with --approve) and approve the action if it is intended."))
        return AgentResponse(run.status, "This needs a human's approval before I run it.",
                             "Nothing was executed.", why)

    def _on_execution_error(self, run: Any) -> AgentResponse:
        error = run.execution.error
        why = [self._error_explanation(error, run)]
        headline = "PostgreSQL rejected the change; nothing was modified."
        if run.plan and run.plan.action_type == "read":
            headline = "The query failed; nothing was returned."
        summary = ("I ran it inside a sandboxed transaction, the database refused it, and the transaction was "
                   "rolled back. This kind of error means the request itself breaks a rule, so I did not retry.")
        return AgentResponse(run.status, headline, summary, why)

    def _on_retry_exhausted(self, run: Any) -> AgentResponse:
        error = run.execution.error if run.execution else None
        why = [Explanation(
            "retry", "RETRY_BUDGET_EXHAUSTED", f"No working query after {run.attempts} attempts",
            "Every attempt was fed the previous database error, but none produced SQL that runs.",
            "Rephrase the request more concretely (exact column values, dates as YYYY-MM-DD).",
            {"attempts": [a for a in getattr(run, "attempt_log", [])]},
        )]
        if error is not None:
            why.append(self._error_explanation(error, run))
        return AgentResponse(run.status, f"I could not produce a query that runs after {run.attempts} attempts.",
                             "Nothing was changed.", why)

    def _on_precommit_failed(self, run: Any) -> AgentResponse:
        result = run.execution
        why = [self._check_explanation(check, run) for check in result.checks if not check.passed]
        action = describe_write(run.plan.sql, result.affected_rows)
        return AgentResponse(
            run.status,
            f"I rolled the change back: {_lower_first(why[0].headline)}.",
            f"In a sandboxed transaction I {action}, but a pre-commit check failed, so it was rolled back and "
            "the database is unchanged. The SQL did what you asked; the request itself breaks a data rule.",
            why,
        )

    def _on_commit_declined(self, run: Any) -> AgentResponse:
        result = run.execution
        why = [Explanation("approval", "COMMIT_DECLINED", "You declined the commit",
                           f"All checks passed for {result.affected_rows} row(s), but the commit was declined.",
                           None)]
        return AgentResponse(run.status, "Not committed: you declined the change.",
                             "The transaction was rolled back; the database is unchanged.", why)

    def _on_dry_run(self, run: Any) -> AgentResponse:
        result = run.execution
        action = describe_write(run.plan.sql, result.affected_rows)
        why = [Explanation("execution", "DRY_RUN", "This was a dry run",
                           f"Every pre-commit check passed, so the change would succeed; it was discarded "
                           "on purpose.", "Run it again without --dry-run to apply it.")]
        return AgentResponse(run.status, f"Dry run OK: it would have {action.split(' where ')[0]}.",
                             f"In a sandbox I {action}; all {len(result.checks)} checks passed; rolled back.", why)

    def _on_gate_refused(self, run: Any) -> AgentResponse:
        reason = run.execution.reason if run.execution else run.answer
        why = [Explanation("execution", "EXECUTION_GATE", "The execution gate refused the plan", reason,
                           "Ask for one SELECT, INSERT, UPDATE or DELETE.")]
        return AgentResponse(run.status, "I did not run this: the execution gate refused it.",
                             "Nothing was sent to the database.", why)

    def _fallback(self, run: Any) -> AgentResponse:
        return AgentResponse(run.status, run.answer or run.status, "")

    # ------------------------------------------------------- root causes
    def _guardrail_reasons(self, guardrail: Any) -> list[Explanation]:
        why = []
        rule_reasons = set()
        for rule in guardrail.triggered_rules:
            kind = rule.get("rule_type", "")
            rule_reasons.add(rule.get("reason"))
            why.append(Explanation("guardrails", kind, RULE_HEADLINE.get(kind, kind),
                                   rule.get("reason", ""), RULE_FIX.get(kind),
                                   {"rule_id": rule.get("rule_id"), "severity": rule.get("severity")}))
        for reason in guardrail.reasons:
            if reason in rule_reasons or reason.startswith("No Week 5") or "no LIMIT" in reason:
                continue
            if "classifier" in reason.lower():
                why.append(Explanation("guardrails", "RISK_CLASSIFIER", "The risk classifier flagged it", reason,
                                       "Make the request more specific; the classifier can only add caution.",
                                       {"label": guardrail.classifier_label,
                                        "probabilities": guardrail.classifier_probabilities}))
            elif reason.startswith("SQL validation failed"):
                why.append(Explanation("guardrails", "SQL_VALIDATION", "The SQL failed schema validation", reason,
                                       "Use table and column names that exist in the schema."))
            else:
                why.append(Explanation("guardrails", "GUARDRAIL", "A guardrail rule fired", reason, None))
        if not why:
            why.append(Explanation("guardrails", "GUARDRAIL", "A guardrail rule fired",
                                   "; ".join(guardrail.reasons), None))
        return why

    def _error_explanation(self, error: Any, run: Any) -> Explanation:
        sqlstate = error.sqlstate or ""
        evidence = {k: v for k, v in {
            "sqlstate": sqlstate, "constraint": error.constraint, "table": error.table,
            "column": error.column, "detail": error.detail, "message": error.message,
        }.items() if v}
        blast = getattr(run, "blast_radius", None)
        predicted = ""
        if blast is not None and blast.predicted_sqlstate and blast.predicted_sqlstate == sqlstate:
            predicted = f" The blast-radius preview had already predicted this: {blast.predicted_failure}."
            evidence["predicted_by_preview"] = True
        rule = CONSTRAINT_TEXT.get(error.constraint or "", None)
        if sqlstate == "23503":
            detail = error.detail or ""
            match = re.search(r"Key \((.+?)\)=\((.+?)\) is (still referenced from|not present in) table \"(\w+)\"",
                              detail)
            if match and match.group(3).startswith("still"):
                cause = (f"{match.group(1)} {match.group(2)} is still used by rows in {match.group(4)}. "
                         f"Rule {error.constraint}: {rule or 'the reference must stay valid'}.")
                fix = f"Delete or reassign the {_noun(match.group(4), 2)} that reference it first."
            elif match:
                cause = f"There is no {_noun(match.group(4), 1)} with {match.group(1)} = {match.group(2)}."
                fix = f"Create that {_noun(match.group(4), 1)} first, or use an existing {match.group(1)}."
            else:
                cause, fix = (rule or error.message) + ".", None
            return Explanation("execution", "FK_VIOLATION", "A foreign-key rule blocked it", cause + predicted, fix,
                               evidence)
        if sqlstate == "23514":
            cause = f"The new value breaks the CHECK constraint {error.constraint}" + (f": {rule}." if rule else ".")
            return Explanation("execution", "CHECK_VIOLATION", "The value is not allowed", cause + predicted,
                               "Use a value that satisfies the rule.", evidence)
        if sqlstate == "23505":
            cause = (error.detail or error.message) + (f" ({rule})." if rule else "")
            return Explanation("execution", "DUPLICATE_KEY", "That key already exists", cause + predicted,
                               "Use a new key, or update the existing row instead.", evidence)
        if sqlstate == "23502":
            return Explanation("execution", "NOT_NULL_VIOLATION", "A required value is missing",
                               f"{error.table}.{error.column} cannot be NULL.{predicted}",
                               "Provide a value for that column.", evidence)
        if sqlstate.startswith("P0"):
            cause = TRIGGER_TEXT.get(error.message, f"A database trigger said: {error.message}")
            return Explanation("execution", "TRIGGER_REJECTED", "A business-rule trigger rejected it",
                               cause[0].upper() + cause[1:] + ".", "Change the request so it follows that rule.",
                               evidence)
        if sqlstate == "42501":
            return Explanation("execution", "PERMISSION_DENIED", "The agent's database role may not do this",
                               error.message, "Ask a DBA; the agent never works around permissions.", evidence)
        if sqlstate.startswith("57"):
            return Explanation("execution", "TIMEOUT", "The statement took too long",
                               f"{error.message} (the sandbox allows 5 s per statement).",
                               "Narrow the request so less data is scanned.", evidence)
        if sqlstate == "25006":
            return Explanation("execution", "READ_ONLY_VIOLATION", "A read tried to change data",
                               "The read ran in a READ ONLY transaction, which PostgreSQL enforced.", None, evidence)
        return Explanation("execution", f"SQLSTATE_{sqlstate or 'UNKNOWN'}", "The database returned an error",
                           f"{error.message}. {error.explanation}".strip(), None, evidence)

    def _check_explanation(self, check: Any, run: Any) -> Explanation:
        evidence = {"check": check.name, "detail": check.detail}
        if check.name.startswith("invariant:"):
            name = check.name.split(":", 1)[1]
            description = INVARIANTS.get(name, (name, ""))[0].rstrip(".")
            numbers = re.search(r"before=(\d+) after=(\d+)", check.detail)
            change = (f" Violations would go from {numbers.group(1)} to {numbers.group(2)}." if numbers else "")
            return Explanation("precommit", f"INVARIANT_{name.upper()}", f"It breaks the rule \"{description}\"",
                               "PostgreSQL itself accepted the statement, but this business rule is checked "
                               f"before COMMIT.{change}",
                               "Change the request so the rule still holds (for example, keep at least one line "
                               "item, or use a cancellation invoice for negative quantities).", evidence)
        if check.name == "rows_affected_nonzero":
            statement = _parse(run.plan.sql)
            return Explanation("precommit", "NOTHING_MATCHED", "Nothing matched the filter",
                               f"No row satisfies {_where_text(statement) or 'the filter'}, so there was nothing "
                               "to commit.", "Check the identifiers in the request.", evidence)
        if check.name == "rows_match_estimate":
            return Explanation("precommit", "ROW_COUNT_MISMATCH", "More or fewer rows changed than estimated",
                               f"{check.detail}; the data changed between the estimate and the write.",
                               "Retry the request.", evidence)
        if check.name == "row_limit":
            return Explanation("precommit", "ROW_LIMIT_EXCEEDED", "Too many rows were changed", check.detail,
                               RULE_FIX["ROW_LIMIT"], evidence)
        return Explanation("precommit", check.name.upper(), f"Pre-commit check {check.name} failed",
                           check.detail, None, evidence)

    # -------------------------------------------------------------- notes
    def _notes(self, run: Any, notes: list[str]) -> list[str]:
        notes = list(notes)
        log = getattr(run, "attempt_log", []) or []
        failed = [a for a in log if a.get("sqlstate")]
        if run.status == "success" and failed:
            first = failed[0]
            notes.append(f"It took {run.attempts} attempts: attempt {first['attempt']} failed with SQLSTATE "
                         f"{first['sqlstate']} ({first['message']}); the error was fed back and the planner "
                         "corrected the SQL.")
        elif run.status == "success" and run.attempts > 1:
            notes.append(f"It took {run.attempts} attempts: the first query returned no rows, so the planner "
                         "re-checked its filter.")
        blast = getattr(run, "blast_radius", None)
        if blast is not None and not blast.error:
            text = "Blast-radius preview: " + blast.summary()
            if blast.prediction_correct is not None:
                text += f" (prediction {'confirmed' if blast.prediction_correct else 'did NOT match'} by execution)"
            notes.append(text + ".")
        drift = getattr(run, "drift", None)
        if drift is not None and drift.level in {"SHIFT", "DRIFT"}:
            notes.append(f"Intent drift {drift.level} (score {drift.score:.2f}): " + " ".join(drift.reasons))
        result = getattr(run, "execution", None)
        if result is not None and result.status == "committed" and result.compensation_sql:
            notes.append(f"Undo SQL is stored in compensation_ledger (audit #{result.audit_id}): "
                         f"{result.compensation_sql[0]}" + (" …" if len(result.compensation_sql) > 1 else ""))
        return notes
