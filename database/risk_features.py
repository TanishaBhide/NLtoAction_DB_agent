"""Shared SQL-derived feature extraction for Week 5 risk scoring."""

from __future__ import annotations

from typing import Any

from sqlglot import exp

try:
    from .sql_validator import SQLValidator
except ImportError:
    from sql_validator import SQLValidator


FEATURE_COLUMNS = [
    "action_type",
    "num_tables",
    "num_joins",
    "has_where",
    "where_is_tautology",
    "touches_sensitive_column",
    "select_star",
    "is_aggregate",
    "estimated_rows",
    "has_limit",
    "has_subquery",
]

DEFAULT_SENSITIVE_COLUMNS = {
    ("customers", "customer_id"),
    ("invoices", "customer_id"),
}


def _literal_value(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Literal):
        return str(expression.this).casefold()
    if isinstance(expression, exp.Boolean):
        return str(expression.this).casefold()
    return None


def has_write_tautology(statement: exp.Expression) -> bool:
    for node in statement.walk():
        if isinstance(node, exp.Boolean) and node.this is True:
            return True
        if isinstance(node, exp.EQ):
            left = _literal_value(node.this)
            right = _literal_value(node.expression)
            if left is not None and left == right:
                return True
            if node.this.sql(dialect="postgres").casefold() == node.expression.sql(dialect="postgres").casefold():
                if isinstance(node.this, exp.Column):
                    return True
        if isinstance(node, exp.Or):
            if any(
                isinstance(child, exp.Boolean) and child.this is True
                for child in node.walk()
            ):
                return True
    return False


def extract_features(
    plan: Any,
    context: dict[str, Any] | None = None,
    validator: SQLValidator | None = None,
) -> dict[str, Any]:
    context = context or {}
    validator = validator or SQLValidator()
    sql = getattr(plan, "sql", "")
    statement, errors = validator._parse_one(sql)
    if errors or statement is None:
        raise ValueError("; ".join(errors) or "SQL did not produce a statement.")
    validation = validator.validate(sql, str(getattr(plan, "action_type", "unknown")), require_limit=False)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    tree = statement
    writes = isinstance(tree, (exp.Insert, exp.Update, exp.Delete))
    action_type = str(getattr(plan, "action_type", "unknown")).casefold()
    if action_type == "ddl":
        action_type = "ddl"
    from_clause_tables = set(validation.tables)
    where = tree.args.get("where")
    sensitive_columns = context.get("sensitive_columns", DEFAULT_SENSITIVE_COLUMNS)
    sensitive_columns = {
        (str(table).casefold(), str(column).casefold())
        for table, column in sensitive_columns
    }
    feature_references = (
        validation.column_references
        if writes
        else validation.projection_column_references
    )
    touches_sensitive = any(
        (table.casefold(), column.casefold()) in sensitive_columns
        for table, column in feature_references
    )
    estimated_rows = context.get("estimated_rows", 0)
    if estimated_rows is None:
        estimated_rows = 0
    if isinstance(estimated_rows, bool) or not isinstance(estimated_rows, (int, float)):
        raise ValueError("estimated_rows must be a number.")
    select_star = bool(validation.wildcard_tables)
    is_aggregate = any(
        isinstance(node, (exp.AggFunc, exp.Group))
        for node in tree.walk()
    )
    return {
        "action_type": action_type if action_type in {"read", "write", "ddl"} else "unknown",
        "num_tables": len(from_clause_tables),
        "num_joins": sum(1 for _ in tree.find_all(exp.Join)),
        "has_where": where is not None,
        "where_is_tautology": has_write_tautology(tree) if writes else False,
        "touches_sensitive_column": touches_sensitive,
        "select_star": select_star,
        "is_aggregate": is_aggregate,
        "estimated_rows": float(estimated_rows),
        "has_limit": tree.args.get("limit") is not None,
        "has_subquery": any(isinstance(node, (exp.Subquery, exp.CTE)) for node in tree.walk()),
    }
