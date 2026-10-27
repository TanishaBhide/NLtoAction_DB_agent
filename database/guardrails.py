"""Fail-closed SQL safety rules and classifier-assisted risk scoring."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

try:
    from .risk_classifier import RiskClassifier
    from .risk_features import DEFAULT_SENSITIVE_COLUMNS, has_write_tautology
    from .sql_generator import ActionPlan, SQLGenerator
    from .sql_validator import SQLValidator
except ImportError:
    from risk_classifier import RiskClassifier
    from risk_features import DEFAULT_SENSITIVE_COLUMNS, has_write_tautology
    from sql_generator import ActionPlan, SQLGenerator
    from sql_validator import SQLValidator


CONFIRM_ROW_THRESHOLD = 100
BLOCK_ROW_THRESHOLD = 1000
READ_WITHOUT_LIMIT_RISK = 8.0
SMALL_IDENTIFIER_RESULT_LIMIT = 25

POLICY_RULES: dict[str, dict[str, Any]] = {
    "WHERE_REQUIRED": {"rule_id": 10001, "severity": "CRITICAL", "weight": 80},
    "ROW_LIMIT": {"rule_id": 10002, "severity": "HIGH", "weight": 45},
    "PII_COLUMN": {"rule_id": 10003, "severity": "HIGH", "weight": 45},
    "DDL_BLOCK": {"rule_id": 10004, "severity": "CRITICAL", "weight": 80},
    "MULTI_STATEMENT": {"rule_id": 10005, "severity": "CRITICAL", "weight": 80},
    "WRITE_TAUTOLOGY": {"rule_id": 10006, "severity": "CRITICAL", "weight": 80},
}
VERDICT_RANK = {"ALLOW": 0, "CONFIRM": 1, "BLOCK": 2}


@dataclass
class GuardrailResult:
    verdict: str
    risk_score: float
    triggered_rules: list[dict[str, Any]] = field(default_factory=list)
    requires_approval: bool = False
    reasons: list[str] = field(default_factory=list)
    classifier_label: str | None = None
    classifier_probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def safe_to_execute(self) -> bool:
        return self.verdict == "ALLOW"


class GuardrailEngine:
    """Evaluate one validated ActionPlan without executing its SQL.

    The risk score is the sum of triggered-rule weights (ROW_LIMIT contributes
    45), plus 8 for an unlimited non-aggregate read, capped at 100. A BLOCK
    floors the score at 80; classifier escalations floor it at 60 (CONFIRM) or
    80 (BLOCK). The classifier can only make a verdict more restrictive.
    """

    def __init__(
        self,
        metadata_path: Path | str | None = None,
        *,
        row_count_provider: Callable[[str], int] | None = None,
        classifier: Any | None = None,
        sensitive_columns: Mapping[tuple[str, str], str] | None = None,
        small_identifier_result_limit: int = SMALL_IDENTIFIER_RESULT_LIMIT,
    ) -> None:
        if isinstance(small_identifier_result_limit, bool) or small_identifier_result_limit < 1:
            raise ValueError("small_identifier_result_limit must be a positive integer.")
        self.metadata_path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.validator = SQLValidator(self.metadata)
        self.row_count_provider = row_count_provider
        self.classifier = classifier
        self.small_identifier_result_limit = small_identifier_result_limit
        self.sensitive_columns = {
            (table.casefold(), column.casefold()): level.upper()
            for (table, column), level in (sensitive_columns or {
                (table, column): "HIGH" for table, column in DEFAULT_SENSITIVE_COLUMNS
            }).items()
        }

    @staticmethod
    def _is_ddl(statement: exp.Expression) -> bool:
        return isinstance(
            statement,
            (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable, exp.Grant, exp.Revoke),
        )

    @staticmethod
    def _has_comments(statement: exp.Expression) -> bool:
        return any(bool(node.comments) for node in statement.walk())

    @staticmethod
    def _where(statement: exp.Expression) -> exp.Expression | None:
        where = statement.args.get("where")
        return where.this if where is not None else None

    @staticmethod
    def _is_aggregate(statement: exp.Expression) -> bool:
        return any(isinstance(node, (exp.AggFunc, exp.Group)) for node in statement.walk())

    def _db_count(self, count_sql: str) -> int | None:
        has_connection = bool(
            os.environ.get("DATABASE_URL")
            or os.environ.get("PGDATABASE")
            or os.environ.get("PGHOST")
        )
        if not has_connection:
            return None
        validation = self.validator.validate(count_sql, "read", require_limit=False)
        if not validation.valid:
            raise ValueError(f"Unsafe count_sql: {'; '.join(validation.errors)}")
        statement, errors = self.validator._parse_one(count_sql)
        if errors or not isinstance(statement, exp.Query):
            raise ValueError("count_sql must be one validated read-only query.")

        import psycopg2

        database_url = os.environ.get("DATABASE_URL")
        options: dict[str, str] = {}
        for variable, option in (
            ("PGHOST", "host"),
            ("PGPORT", "port"),
            ("PGDATABASE", "dbname"),
            ("PGUSER", "user"),
            ("PGPASSWORD", "password"),
        ):
            value = os.environ.get(variable)
            if value:
                options[option] = value
        connection = psycopg2.connect(database_url) if database_url else psycopg2.connect(**options)
        try:
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute(count_sql)
                row = cursor.fetchone()
                if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
                    raise ValueError("count_sql did not return one integer row count.")
                connection.rollback()
                return row[0]
        finally:
            connection.close()

    def _row_count(
        self,
        plan: Any,
        row_count: int | None,
        context: Mapping[str, Any],
        statement: exp.Expression,
    ) -> int | None:
        if row_count is not None:
            count = row_count
        elif context.get("estimated_rows") is not None:
            count = context["estimated_rows"]
        elif context.get("row_count") is not None:
            count = context["row_count"]
        elif isinstance(statement, exp.Insert):
            source = statement.expression
            count = len(source.expressions) if isinstance(source, exp.Values) else None
        elif getattr(plan, "count_sql", None):
            provider = context.get("row_count_provider") or self.row_count_provider
            if context.get("no_db") and provider is None:
                count = None
            else:
                count_statement, count_errors = self.validator._parse_one(plan.count_sql)
                if count_errors or count_statement is None or self._has_comments(count_statement):
                    raise ValueError("Unsafe count_sql: comments and parse errors are not allowed.")
                count_validation = self.validator.validate_count_sql(plan.count_sql, plan.sql)
                if not count_validation.valid:
                    raise ValueError("Unsafe count_sql: " + "; ".join(count_validation.errors))
                count = provider(plan.count_sql) if provider else self._db_count(plan.count_sql)
        else:
            count = None
        if count is None:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("The estimated row count must be a non-negative integer.")
        return count

    def _rule(
        self,
        rule_type: str,
        reason: str,
        triggered: dict[str, dict[str, Any]],
        reasons: list[str],
    ) -> None:
        definition = POLICY_RULES[rule_type]
        triggered.setdefault(
            rule_type,
            {
                "rule_id": definition["rule_id"],
                "rule_type": rule_type,
                "severity": definition["severity"],
                "reason": reason,
            },
        )
        if reason not in reasons:
            reasons.append(reason)

    def evaluate(
        self,
        plan: ActionPlan | Any,
        *,
        row_count: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> GuardrailResult:
        context = context or {}
        triggered: dict[str, dict[str, Any]] = {}
        reasons: list[str] = []
        score = 0.0
        verdict = "ALLOW"
        try:
            if getattr(plan, "blocked", False):
                reason = getattr(plan, "block_reason", None) or "The action plan is already blocked."
                reasons.append(reason)
                verdict = "BLOCK"
                score = 100.0
                parsed = None
                validation = None
            else:
                sql = getattr(plan, "sql", "")
                statements = [node for node in sqlglot.parse(sql, read="postgres") if node is not None]
                if len(statements) != 1:
                    self._rule(
                        "MULTI_STATEMENT",
                        "SQL must contain exactly one statement.",
                        triggered,
                        reasons,
                    )
                    verdict = "BLOCK"
                    score = 100.0
                    parsed = None
                    validation = None
                else:
                    parsed = statements[0]
                    if self._has_comments(parsed):
                        self._rule(
                            "MULTI_STATEMENT",
                            "SQL comments are not allowed because they can hide query logic.",
                            triggered,
                            reasons,
                        )
                        verdict = "BLOCK"
                    action_type = str(getattr(plan, "action_type", "unknown")).casefold()
                    if action_type == "ddl" or self._is_ddl(parsed):
                        self._rule(
                            "DDL_BLOCK",
                            "DDL and privilege-changing statements are always blocked.",
                            triggered,
                            reasons,
                        )
                        verdict = "BLOCK"
                    validation = self.validator.validate(
                        sql,
                        action_type,
                        require_limit=(
                            action_type == "read" and parsed.args.get("limit") is not None
                        ),
                    )
                    where_expression = self._where(parsed)
                    if isinstance(parsed, (exp.Update, exp.Delete)) and where_expression is None:
                        self._rule(
                            "WHERE_REQUIRED",
                            f"{type(parsed).__name__.upper()} requires a WHERE clause.",
                            triggered,
                            reasons,
                        )
                        verdict = "BLOCK"
                    if isinstance(parsed, (exp.Update, exp.Delete)) and has_write_tautology(parsed):
                        self._rule(
                            "WRITE_TAUTOLOGY",
                            "Write WHERE contains a tautology that could match every row.",
                            triggered,
                            reasons,
                        )
                        verdict = "BLOCK"
                    if not validation.valid:
                        reason = "SQL validation failed: " + "; ".join(validation.errors)
                        if reason not in reasons:
                            reasons.append(reason)
                        verdict = "BLOCK"
                    if verdict != "BLOCK":
                        self._sensitivity_rules(parsed, validation, where_expression, triggered, reasons)
                        if "PII_COLUMN" in triggered:
                            verdict = "CONFIRM"
                        count = self._row_count(plan, row_count, context, parsed)
                        if count is not None:
                            if count > BLOCK_ROW_THRESHOLD:
                                self._rule(
                                    "ROW_LIMIT",
                                    f"Estimated affected rows {count} exceed the block threshold {BLOCK_ROW_THRESHOLD}.",
                                    triggered,
                                    reasons,
                                )
                                verdict = "BLOCK"
                            elif count > CONFIRM_ROW_THRESHOLD:
                                self._rule(
                                    "ROW_LIMIT",
                                    f"Estimated affected rows {count} exceed the confirmation threshold {CONFIRM_ROW_THRESHOLD}.",
                                    triggered,
                                    reasons,
                                )
                                if verdict != "BLOCK":
                                    verdict = "CONFIRM"
                        elif action_type == "write":
                            self._rule(
                                "ROW_LIMIT",
                                "Affected row count is unavailable; confirmation is required.",
                                triggered,
                                reasons,
                            )
                            if verdict != "BLOCK":
                                verdict = "CONFIRM"
                        has_limit = parsed.args.get("limit") is not None
                        if action_type == "read" and not has_limit and not self._is_aggregate(parsed):
                            reasons.append("Read query has no LIMIT; this warning increases risk.")
                            score += READ_WITHOUT_LIMIT_RISK
            for item in triggered.values():
                score += float(POLICY_RULES[item["rule_type"]]["weight"])
            if verdict == "BLOCK":
                score = max(score, 80.0)

            classifier_label: str | None = None
            probabilities: dict[str, float] = {}
            if (
                verdict != "BLOCK"
                and not getattr(plan, "blocked", False)
                and parsed is not None
                and validation is not None
                and validation.valid
            ):
                classifier_context = dict(context)
                classifier_context.setdefault("sensitive_columns", self.sensitive_columns)
                if row_count is not None:
                    classifier_context["estimated_rows"] = row_count
                classifier = self.classifier or RiskClassifier(validator=self.validator)
                classifier_label, probabilities = classifier.predict(plan, classifier_context)
                top_probability = max(probabilities.values(), default=0.0)
                if top_probability < 0.6:
                    classifier_label = "CONFIRM"
                    reasons.append("Classifier confidence is below 0.60; escalated to confirmation.")
                classifier_verdict = classifier_label.upper()
                if classifier_verdict not in VERDICT_RANK:
                    raise ValueError(f"Classifier returned unsupported label {classifier_label!r}.")
                if VERDICT_RANK[classifier_verdict] > VERDICT_RANK[verdict]:
                    verdict = classifier_verdict
                    reasons.append(f"Risk classifier escalated the verdict to {verdict}.")
                    score = max(score, 80.0 if verdict == "BLOCK" else 60.0)
            if not reasons:
                reasons.append("No Week 5 guardrail rule was triggered.")
            return GuardrailResult(
                verdict=verdict,
                risk_score=round(min(score, 100.0), 2),
                triggered_rules=list(triggered.values()),
                requires_approval=verdict == "CONFIRM",
                reasons=reasons,
                classifier_label=classifier_label,
                classifier_probabilities=probabilities,
            )
        except Exception as error:
            failure = f"Guardrail evaluation failed closed: {type(error).__name__}: {error}"
            return GuardrailResult(
                verdict="BLOCK",
                risk_score=100.0,
                triggered_rules=list(triggered.values()),
                requires_approval=False,
                reasons=[*reasons, failure],
            )

    def _sensitivity_rules(
        self,
        statement: exp.Expression,
        validation: Any,
        where_expression: exp.Expression | None,
        triggered: dict[str, dict[str, Any]],
        reasons: list[str],
    ) -> None:
        action_type = "write" if isinstance(statement, (exp.Insert, exp.Update, exp.Delete)) else "read"
        high_columns = {
            key for key, level in self.sensitive_columns.items() if level == "HIGH"
        }
        if action_type == "read":
            wildcard_high = [
                table
                for table in validation.wildcard_tables
                if any(sensitive_table == table.casefold() for sensitive_table, _ in high_columns)
            ]
            if wildcard_high:
                self._rule(
                    "PII_COLUMN",
                    "SELECT * exposes a table with HIGH-sensitivity columns: " + ", ".join(wildcard_high) + ".",
                    triggered,
                    reasons,
                )
            raw_projection = {
                (table.casefold(), column.casefold())
                for table, column in validation.projection_column_references
            }
            exposed = sorted(raw_projection & high_columns)
            limit = statement.args.get("limit")
            limit_value = None
            if limit is not None and limit.expression is not None and limit.expression.is_int:
                limit_value = int(limit.expression.this)
            has_restrictive_filter = (
                where_expression is not None
                and not has_write_tautology(where_expression)
            )
            safe_explicit_result = (
                bool(exposed)
                and not validation.wildcard_tables
                and limit_value is not None
                and limit_value > 0
                and has_restrictive_filter
            )
            if exposed and not safe_explicit_result:
                formatted = ", ".join(f"{table}.{column}" for table, column in exposed)
                self._rule(
                    "PII_COLUMN",
                    f"HIGH-sensitivity raw columns {formatted} require a restrictive WHERE "
                    f"and a positive LIMIT.",
                    triggered,
                    reasons,
                )
        else:
            touched = sorted(
                {
                    (table.casefold(), column.casefold())
                    for table, column in validation.column_references
                }
                & high_columns
            )
            if touched:
                formatted = ", ".join(f"{table}.{column}" for table, column in touched)
                self._rule(
                    "PII_COLUMN",
                    f"Write touches HIGH-sensitivity columns: {formatted}.",
                    triggered,
                    reasons,
                )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SQL action-plan safety without executing SQL.")
    parser.add_argument("request", help="Natural-language database request")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--no-db", action="store_true", help="Do not use PostgreSQL for row counts or persistence")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        generator = SQLGenerator()
        plan = generator.generate(args.request, session_id=args.session_id)
        row_provider = None
        if not args.no_db:
            has_db = bool(os.environ.get("DATABASE_URL") or os.environ.get("PGDATABASE"))
            if has_db:
                row_provider = None
        engine = GuardrailEngine(row_count_provider=row_provider)
        result = engine.evaluate(
            plan,
            context={"session_id": args.session_id, "no_db": args.no_db},
        )
        if not args.no_db:
            try:
                from .guardrail_store import GuardrailStore
            except ImportError:
                from guardrail_store import GuardrailStore
            GuardrailStore().save_assessment(args.session_id, result)
        else:
            try:
                from .guardrail_store import GuardrailStore
            except ImportError:
                from guardrail_store import GuardrailStore

            GuardrailStore(in_memory=True).save_assessment(args.session_id, result)
        print(json.dumps(asdict(result), indent=2))
        return 0 if result.verdict != "BLOCK" else 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "verdict": "BLOCK",
                    "risk_score": 100.0,
                    "triggered_rules": [],
                    "requires_approval": False,
                    "reasons": [f"Guardrail CLI failed: {type(error).__name__}: {error}"],
                    "classifier_label": None,
                    "classifier_probabilities": {},
                },
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
