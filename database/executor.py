"""Week 6: safe database execution engine.

Runs ONE guardrail-approved statement inside an explicit, sandboxed PostgreSQL
transaction:

    gate         plan not blocked; Week 5 verdict ALLOW, or CONFIRM + approval;
                 statement re-parsed: exactly one SELECT/INSERT/UPDATE/DELETE
    BEGIN        READ ONLY for reads, READ WRITE for writes
    SET LOCAL    statement_timeout / lock_timeout bound the sandbox
    snapshot     expected row count + invariant counts + before-image
    SAVEPOINT    then the write itself (with RETURNING * to capture rows)
    checks       pre-commit row and constraint checks (see PrecommitCheck)
    COMMIT       only if every check passed, it is not a dry run and the
                 commit decider agrees; otherwise ROLLBACK

Reads are always rolled back (nothing to commit). Every database error is
captured as a structured ExecutionError (SQLSTATE, constraint, table, hint,
retryable flag) and the transaction is rolled back. Each attempt is written to
execution_audit (and compensation_ledger for commits) on a separate
connection, so the audit row survives the rollback it describes.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import sqlglot
from sqlglot import exp

try:
    from .db import connect
    from .guardrails import BLOCK_ROW_THRESHOLD, CONFIRM_ROW_THRESHOLD
    from .sql_validator import SQLValidator
except ImportError:
    from db import connect
    from guardrails import BLOCK_ROW_THRESHOLD, CONFIRM_ROW_THRESHOLD
    from sql_validator import SQLValidator


STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT_MS = 2000
MAX_RESULT_ROWS = 100
MAX_COMPENSATION_ROWS = 100

# Business invariants that PostgreSQL constraints alone do not enforce. Each
# query counts violations; a write fails its pre-commit check if it makes any
# count go UP (rows that were already violating do not block unrelated writes).
INVARIANTS: dict[str, tuple[str, str]] = {
    "invoices_keep_line_items": (
        "Every invoice keeps at least one line item.",
        "SELECT COUNT(*) FROM invoices AS i WHERE NOT EXISTS "
        "(SELECT 1 FROM invoice_items AS ii WHERE ii.invoice_no = i.invoice_no)",
    ),
    "negative_qty_only_on_cancellations": (
        "Negative quantities appear only on cancelled invoices.",
        "SELECT COUNT(*) FROM invoice_items AS ii JOIN invoices AS i "
        "ON i.invoice_no = ii.invoice_no WHERE ii.quantity < 0 AND i.status <> 'cancelled'",
    ),
    "negative_price_only_on_adjustments": (
        "Negative unit prices appear only on adjustment invoices.",
        "SELECT COUNT(*) FROM invoice_items AS ii JOIN invoices AS i "
        "ON i.invoice_no = ii.invoice_no WHERE ii.unit_price < 0 AND i.status <> 'adjustment'",
    ),
    "status_matches_invoice_prefix": (
        "Invoice status agrees with the C/A invoice-number prefix.",
        "SELECT COUNT(*) FROM invoices WHERE (invoice_no LIKE 'C%' AND status <> 'cancelled') "
        "OR (invoice_no LIKE 'A%' AND status <> 'adjustment')",
    ),
}

# SQLSTATE class -> (category, retryable by regenerating SQL, plain explanation)
SQLSTATE_CLASSES: dict[str, tuple[str, bool, str]] = {
    "42": ("syntax_or_semantic", True, "The SQL is malformed or references an invalid type, column or operator."),
    "22": ("data_exception", True, "A literal or expression has the wrong format or type for the column."),
    "23": ("integrity_violation", False, "The change would break a database constraint; the request itself is invalid."),
    "P0": ("business_rule_trigger", False, "A database trigger rejected the change as a business-rule violation."),
    "40": ("transaction_conflict", True, "The transaction conflicted with a concurrent one; it can be retried."),
    "55": ("lock_unavailable", True, "A needed lock was not available within the lock timeout."),
    "57": ("cancelled_or_timeout", False, "The statement was cancelled, usually by the statement timeout."),
    "08": ("connection", False, "The database connection failed."),
    "25": ("transaction_state", False, "The statement is not allowed in this transaction state."),
}
NON_RETRYABLE_SQLSTATES = {"42501"}  # insufficient_privilege: never retry around permissions


@dataclass
class ExecutionError:
    sqlstate: str | None
    error_name: str
    category: str
    message: str
    detail: str | None = None
    hint: str | None = None
    constraint: str | None = None
    table: str | None = None
    column: str | None = None
    retryable: bool = False
    explanation: str = ""

    def feedback(self) -> str:
        """Compact text for the LLM in the next ReAct attempt."""
        parts = [f"PostgreSQL error {self.sqlstate} ({self.error_name}): {self.message}"]
        for label, value in (
            ("detail", self.detail),
            ("hint", self.hint),
            ("constraint", self.constraint),
            ("table", self.table),
            ("column", self.column),
        ):
            if value:
                parts.append(f"{label}: {value}")
        return "\n".join(parts)


@dataclass
class PrecommitCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class TxStep:
    sql: str
    outcome: str
    ms: float


@dataclass
class ExecutionResult:
    status: str  # read_only | committed | rolled_back | failed | not_executed
    action_type: str
    sql: str
    reason: str
    executed_sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    affected_rows: int | None = None
    expected_rows: int | None = None
    transaction_id: str | None = None
    checks: list[PrecommitCheck] = field(default_factory=list)
    tx_log: list[TxStep] = field(default_factory=list)
    error: ExecutionError | None = None
    compensation_sql: list[str] = field(default_factory=list)
    dry_run: bool = False
    duration_ms: float = 0.0
    audit_id: int | None = None
    audit_warning: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"read_only", "committed"}

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), default=_json_default))


class PrecommitFailure(Exception):
    """Raised inside the transaction when a pre-commit check fails."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def capture_error(error: BaseException) -> ExecutionError:
    """Turn a psycopg2 (or any) exception into a structured ExecutionError."""
    sqlstate = getattr(error, "pgcode", None)
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    message = (primary or str(error)).strip().splitlines()[0] if (primary or str(error)) else type(error).__name__
    category, retryable, explanation = ("unknown", False, "Unexpected execution failure.")
    if sqlstate:
        category, retryable, explanation = SQLSTATE_CLASSES.get(sqlstate[:2], (category, retryable, explanation))
        if sqlstate in NON_RETRYABLE_SQLSTATES:
            category, retryable, explanation = ("permission", False, "The database role lacks permission.")
    return ExecutionError(
        sqlstate=sqlstate,
        error_name=type(error).__name__,
        category=category,
        message=message,
        detail=getattr(diag, "message_detail", None) if diag else None,
        hint=getattr(diag, "message_hint", None) if diag else None,
        constraint=getattr(diag, "constraint_name", None) if diag else None,
        table=getattr(diag, "table_name", None) if diag else None,
        column=getattr(diag, "column_name", None) if diag else None,
        retryable=retryable,
        explanation=explanation,
    )


class SafeExecutor:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        metadata_path: Path | str | None = None,
        statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = LOCK_TIMEOUT_MS,
        max_result_rows: int = MAX_RESULT_ROWS,
        audit: bool = True,
        invariants: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.database_url = database_url
        path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        self.metadata = json.loads(path.read_text(encoding="utf-8"))
        self.validator = SQLValidator(self.metadata)
        self.primary_keys = {
            str(table["name"]).casefold(): [str(column) for column in table.get("primary_key", [])]
            for table in self.metadata.get("tables", [])
        }
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.lock_timeout_ms = int(lock_timeout_ms)
        self.max_result_rows = max_result_rows
        self.audit = audit
        self.invariants = INVARIANTS if invariants is None else invariants

    # ------------------------------------------------------------------ gate
    def _gate(self, plan: Any, guardrail: Any, approved: bool) -> tuple[str | None, exp.Expression | None]:
        if getattr(plan, "blocked", False):
            return f"Plan is blocked: {getattr(plan, 'block_reason', '')}", None
        verdict = str(getattr(guardrail, "verdict", "BLOCK")).upper()
        if verdict == "BLOCK":
            return "Week 5 guardrail verdict is BLOCK; execution refused.", None
        if verdict == "CONFIRM" and not approved:
            return "Week 5 guardrail verdict is CONFIRM and no human approval was given.", None
        if verdict not in {"ALLOW", "CONFIRM"}:
            return f"Unknown guardrail verdict {verdict!r}; execution refused.", None
        sql = getattr(plan, "sql", "") or ""
        statement, errors = self.validator._parse_one(sql)
        if errors or statement is None:
            return "Execution gate: " + "; ".join(errors or ["SQL did not parse."]), None
        action_type = str(getattr(plan, "action_type", ""))
        if action_type == "read" and not isinstance(statement, exp.Query):
            return "Execution gate: a read plan must be a single SELECT query.", None
        if action_type == "write" and not isinstance(statement, (exp.Insert, exp.Update, exp.Delete)):
            return "Execution gate: a write plan must be a single INSERT, UPDATE or DELETE.", None
        if action_type not in {"read", "write"}:
            return f"Execution gate: action type {action_type!r} is never executed.", None
        if any(isinstance(node, (exp.Command, exp.Transaction, exp.Commit, exp.Rollback)) for node in statement.walk()):
            return "Execution gate: transaction-control or raw commands are not allowed.", None
        return None, statement

    # --------------------------------------------------------------- helpers
    def _run(self, cursor: Any, log: list[TxStep], sql: str, params: Any = None, *, show: str | None = None) -> None:
        started = time.perf_counter()
        try:
            cursor.execute(sql, params)
        except Exception as error:
            log.append(TxStep(show or sql, f"ERROR {getattr(error, 'pgcode', '') or type(error).__name__}", _ms(started)))
            raise
        outcome = cursor.statusmessage or "OK"
        log.append(TxStep(show or sql, outcome, _ms(started)))

    def _scalar(self, cursor: Any, log: list[TxStep], sql: str, *, show: str | None = None) -> Any:
        self._run(cursor, log, sql, show=show)
        row = cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _target_table(statement: exp.Expression) -> str:
        target = statement.this
        if isinstance(target, exp.Schema):  # INSERT INTO t (cols)
            target = target.this
        if not isinstance(target, exp.Table):
            raise ValueError("Could not determine the write's target table.")
        return target.name

    def _before_image_sql(self, statement: exp.Expression) -> str | None:
        if not isinstance(statement, (exp.Update, exp.Delete)):
            return None
        table = self._target_table(statement)
        select = exp.select("*").from_(table).limit(BLOCK_ROW_THRESHOLD + 1)
        where = statement.args.get("where")
        if where is not None:
            select = select.where(where.this.copy())
        return select.sql(dialect="postgres")

    @staticmethod
    def _with_returning(statement: exp.Expression) -> str:
        statement = statement.copy()
        if statement.args.get("returning") is None:
            statement.set("returning", exp.Returning(expressions=[exp.Star()]))
        return statement.sql(dialect="postgres")

    @staticmethod
    def _expected_insert_rows(statement: exp.Insert) -> int | None:
        source = statement.expression
        return len(source.expressions) if isinstance(source, exp.Values) else None

    def _compensation(
        self,
        cursor: Any,
        statement: exp.Expression,
        columns: list[str],
        before: list[tuple],
        after: list[tuple],
    ) -> list[str]:
        """Build SQL that would undo the committed change (stored, never auto-run)."""
        table = self._target_table(statement)
        keys = self.primary_keys.get(table.casefold(), [])
        if not keys or len(before) > MAX_COMPENSATION_ROWS or len(after) > MAX_COMPENSATION_ROWS:
            return []
        index = {name: position for position, name in enumerate(columns)}
        if any(key not in index for key in keys):
            return []

        def literal(value: Any) -> str:
            return cursor.mogrify("%s", (value,)).decode()

        def key_predicate(row: tuple) -> str:
            return " AND ".join(f"{key} = {literal(row[index[key]])}" for key in keys)

        statements: list[str] = []
        if isinstance(statement, exp.Insert):
            statements = [f"DELETE FROM {table} WHERE {key_predicate(row)}" for row in after]
        elif isinstance(statement, exp.Delete):
            column_list = ", ".join(columns)
            statements = [
                f"INSERT INTO {table} ({column_list}) VALUES ({', '.join(literal(value) for value in row)})"
                for row in before
            ]
        elif isinstance(statement, exp.Update):
            changed = [
                assignment.this.name
                for assignment in statement.expressions
                if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column)
            ]
            if not changed or any(column in keys for column in changed):
                return []
            statements = [
                f"UPDATE {table} SET "
                + ", ".join(f"{column} = {literal(row[index[column]])}" for column in changed if column in index)
                + f" WHERE {key_predicate(row)}"
                for row in before
            ]
        return statements

    # --------------------------------------------------------------- execute
    def execute(
        self,
        plan: Any,
        guardrail: Any,
        *,
        approved: bool = False,
        dry_run: bool = False,
        commit_decider: Callable[[ExecutionResult], bool] | None = None,
        session_id: str | None = None,
        request_text: str | None = None,
        attempt_no: int = 1,
    ) -> ExecutionResult:
        started = time.perf_counter()
        action_type = str(getattr(plan, "action_type", "unknown"))
        sql = (getattr(plan, "sql", "") or "").strip()
        result = ExecutionResult(status="not_executed", action_type=action_type, sql=sql, reason="", dry_run=dry_run)

        refusal, statement = self._gate(plan, guardrail, approved)
        if refusal:
            result.reason = refusal
            result.duration_ms = _ms(started)
            self._write_audit(result, guardrail, session_id, request_text, attempt_no)
            return result
        assert statement is not None

        connection = None
        log = result.tx_log
        in_transaction = False
        try:
            connection = connect(self.database_url)
            connection.autocommit = True  # we issue BEGIN/COMMIT/ROLLBACK ourselves
            with connection.cursor() as cursor:
                mode = "READ ONLY" if action_type == "read" else "READ WRITE"
                self._run(cursor, log, f"BEGIN ISOLATION LEVEL READ COMMITTED {mode}")
                in_transaction = True
                self._run(cursor, log, f"SET LOCAL statement_timeout = {self.statement_timeout_ms}")
                self._run(cursor, log, f"SET LOCAL lock_timeout = {self.lock_timeout_ms}")
                if action_type == "read":
                    self._execute_read(cursor, log, result, sql)
                    self._run(cursor, log, "ROLLBACK")
                    in_transaction = False
                    result.status = "read_only"
                    result.reason = (
                        f"Read returned {result.row_count} row(s) in a READ ONLY transaction "
                        "(rolled back: nothing to commit)."
                    )
                else:
                    self._execute_write(cursor, log, result, plan, statement, guardrail, approved)
                    all_passed = all(check.passed for check in result.checks)
                    if not all_passed:
                        failed = ", ".join(check.name for check in result.checks if not check.passed)
                        raise PrecommitFailure(f"Pre-commit check(s) failed: {failed}.")
                    if dry_run:
                        decision, why = False, "Dry run: every check passed, changes discarded."
                    elif commit_decider is not None:
                        decision = bool(commit_decider(result))
                        why = ("All pre-commit checks passed; committed." if decision
                               else "Commit declined; rolled back, the database is unchanged.")
                    else:
                        decision, why = True, "All pre-commit checks passed; committed."
                    self._run(cursor, log, "COMMIT" if decision else "ROLLBACK")
                    in_transaction = False
                    result.status = "committed" if decision else "rolled_back"
                    result.reason = why
                    if not decision:
                        result.compensation_sql = []
        except PrecommitFailure as failure:
            self._rollback(connection, log, in_transaction)
            result.status = "rolled_back"
            result.reason = f"{failure} Transaction rolled back; the database is unchanged."
            result.compensation_sql = []
        except Exception as error:  # noqa: BLE001 - every DB error is captured
            self._rollback(connection, log, in_transaction)
            result.error = capture_error(error)
            result.status = "failed"
            result.reason = (
                f"{result.error.category}: {result.error.message}. "
                "Transaction rolled back; the database is unchanged."
            )
            result.compensation_sql = []
        finally:
            if connection is not None:
                connection.close()
        result.duration_ms = _ms(started)
        self._write_audit(result, guardrail, session_id, request_text, attempt_no)
        return result

    @staticmethod
    def _rollback(connection: Any, log: list[TxStep], in_transaction: bool) -> None:
        if connection is None or not in_transaction:
            return
        started = time.perf_counter()
        try:
            with connection.cursor() as cursor:
                cursor.execute("ROLLBACK")
            log.append(TxStep("ROLLBACK", "ROLLBACK", _ms(started)))
        except Exception as error:  # noqa: BLE001
            log.append(TxStep("ROLLBACK", f"ERROR {error}", _ms(started)))

    def _execute_read(self, cursor: Any, log: list[TxStep], result: ExecutionResult, sql: str) -> None:
        vxid = self._scalar(
            cursor, log,
            "SELECT virtualtransaction FROM pg_locks WHERE pid = pg_backend_pid() "
            "AND locktype = 'virtualxid' LIMIT 1",
        )
        result.transaction_id = f"vxid {vxid}"  # read-only transactions get no real txid
        result.executed_sql = sql
        self._run(cursor, log, sql)
        result.columns = [description[0] for description in cursor.description or []]
        rows = cursor.fetchmany(self.max_result_rows + 1)
        result.truncated = len(rows) > self.max_result_rows
        result.rows = [list(row) for row in rows[: self.max_result_rows]]
        result.row_count = len(result.rows)

    def _execute_write(
        self,
        cursor: Any,
        log: list[TxStep],
        result: ExecutionResult,
        plan: Any,
        statement: exp.Expression,
        guardrail: Any,
        approved: bool,
    ) -> None:
        checks = result.checks
        result.transaction_id = str(self._scalar(cursor, log, "SELECT txid_current()"))

        # 1. Snapshot inside the same transaction, before the write.
        if isinstance(statement, exp.Insert):
            expected = self._expected_insert_rows(statement)
        else:
            count_sql = getattr(plan, "count_sql", None)
            count_check = self.validator.validate_count_sql(count_sql or "", plan.sql)
            if not count_sql or not count_check.valid:
                raise PrecommitFailure("count_sql is missing or does not match the write.")
            expected = self._scalar(cursor, log, count_sql)
        result.expected_rows = expected
        invariants_before = {
            name: self._scalar(cursor, log, query, show=f"-- invariant {name} (before)\n{query}")
            for name, (_, query) in self.invariants.items()
        }
        before_columns: list[str] = []
        before_rows: list[tuple] = []
        before_sql = self._before_image_sql(statement)
        if before_sql:
            self._run(cursor, log, before_sql, show=f"-- before-image\n{before_sql}")
            before_columns = [description[0] for description in cursor.description or []]
            before_rows = cursor.fetchall()

        # 2. The write itself, behind a savepoint, capturing affected rows.
        self._run(cursor, log, "SAVEPOINT agent_write")
        executed = self._with_returning(statement)
        result.executed_sql = executed
        self._run(cursor, log, executed)
        affected = cursor.rowcount
        returned_columns = [description[0] for description in cursor.description or []]
        returned_rows = cursor.fetchall() if cursor.description else []
        result.affected_rows = affected
        result.columns = returned_columns
        result.rows = [list(row) for row in returned_rows[: self.max_result_rows]]
        result.row_count = len(result.rows)

        # 3. Pre-commit checks.
        try:
            self._run(cursor, log, "SET CONSTRAINTS ALL IMMEDIATE")
            checks.append(PrecommitCheck("deferred_constraints", True, "All deferrable constraints validated now."))
        except Exception:
            checks.append(PrecommitCheck("deferred_constraints", False, "A deferred constraint failed."))
            raise
        checks.append(
            PrecommitCheck(
                "rows_affected_nonzero",
                affected > 0,
                f"{affected} row(s) affected" + ("" if affected > 0 else "; nothing matched, nothing to commit"),
            )
        )
        if expected is not None:
            checks.append(
                PrecommitCheck(
                    "rows_match_estimate",
                    affected == expected,
                    f"estimated {expected} by count_sql, actually affected {affected}",
                )
            )
        verdict = str(getattr(guardrail, "verdict", "")).upper()
        limit = BLOCK_ROW_THRESHOLD if (approved and verdict == "CONFIRM") else CONFIRM_ROW_THRESHOLD
        checks.append(
            PrecommitCheck(
                "row_limit",
                affected <= limit,
                f"{affected} <= {limit} ({'approved' if limit == BLOCK_ROW_THRESHOLD else 'unapproved'} write limit)",
            )
        )
        for name, (description, query) in self.invariants.items():
            after = self._scalar(cursor, log, query, show=f"-- invariant {name} (after)\n{query}")
            before = invariants_before[name]
            checks.append(
                PrecommitCheck(
                    f"invariant:{name}",
                    after <= before,
                    f"{description} violations before={before} after={after}",
                )
            )
        if all(check.passed for check in checks):
            self._run(cursor, log, "RELEASE SAVEPOINT agent_write")
            result.compensation_sql = self._compensation(
                cursor, statement, returned_columns or before_columns, before_rows, returned_rows
            )

    # ----------------------------------------------------------------- audit
    def _write_audit(
        self,
        result: ExecutionResult,
        guardrail: Any,
        session_id: str | None,
        request_text: str | None,
        attempt_no: int,
    ) -> None:
        if not self.audit:
            return
        try:
            connection = connect(self.database_url)
        except Exception as error:  # noqa: BLE001
            result.audit_warning = f"audit skipped: {error}"
            return
        try:
            with connection, connection.cursor() as cursor:
                if session_id:
                    cursor.execute(
                        "INSERT INTO agent_sessions (session_id, purpose) VALUES (%s, %s) "
                        "ON CONFLICT (session_id) DO NOTHING",
                        (session_id[:36], "NL-to-Action agent session"),
                    )
                cursor.execute(
                    """
                    INSERT INTO execution_audit (
                        session_id, intent_text, generated_sql, guardrail_verdict, execution_status,
                        risk_score, error_message, transaction_id, affected_row_count, sqlstate,
                        error_category, precommit_checks, transaction_log, attempt_no, duration_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING audit_id
                    """,
                    (
                        session_id[:36] if session_id else None,
                        request_text or "",
                        result.sql or None,
                        str(getattr(guardrail, "verdict", "BLOCK")).lower(),
                        result.status,
                        getattr(guardrail, "risk_score", None),
                        result.error.message if result.error else (None if result.succeeded else result.reason),
                        result.transaction_id,
                        result.affected_rows or 0,
                        result.error.sqlstate if result.error else None,
                        result.error.category if result.error else None,
                        json.dumps([asdict(check) for check in result.checks]),
                        json.dumps([asdict(step) for step in result.tx_log]),
                        attempt_no,
                        result.duration_ms,
                    ),
                )
                result.audit_id = int(cursor.fetchone()[0])
                if result.status == "committed":
                    for compensation in result.compensation_sql or ["-- no compensation could be derived"]:
                        cursor.execute(
                            "INSERT INTO compensation_ledger (audit_id, compensation_sql, compensation_status) "
                            "VALUES (%s, %s, %s)",
                            (
                                result.audit_id,
                                compensation,
                                "pending" if result.compensation_sql else "not_required",
                            ),
                        )
        except Exception as error:  # noqa: BLE001 - audit must never mask the result
            result.audit_warning = f"audit write failed: {type(error).__name__}: {error}"
        finally:
            connection.close()


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
