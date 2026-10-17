"""Week 4: generate parameterized PostgreSQL SQL from a Week 3 ParsedIntent.

This module generates a plan only. It NEVER connects to PostgreSQL or executes SQL.
Run from the repository root with:
    python database/sql_agent.py "show customers from France"
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from .intent_parser import IntentParser, ParsedIntent
except ImportError:  # supports `python database/sql_agent.py ...`
    from intent_parser import IntentParser, ParsedIntent


DEFAULT_METADATA = Path(__file__).with_name("schema_metadata.json")
# Explicitly supported joins for this project's operational schema.
JOIN_SQL = {
    frozenset({"customers", "invoices"}):
        "customers.customer_id = invoices.customer_id",
    frozenset({"invoices", "invoice_items"}):
        "invoices.invoice_no = invoice_items.invoice_no",
    frozenset({"products", "invoice_items"}):
        "products.stock_code = invoice_items.stock_code",
}


@dataclass
class SQLAction:
    action_type: str
    intent: str
    sql: str | None
    params: list[Any]
    tables: list[str]
    columns: list[str]
    requires_approval: bool
    generated: bool
    message: str
    raw_text: str
    confidence: float


class SQLGenerationError(ValueError):
    pass


class SQLAgent:
    def __init__(self, metadata_path: Path | str = DEFAULT_METADATA):
        self.metadata_path = Path(metadata_path)
        self.schema = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.tables: dict[str, dict[str, Any]] = {
            t["name"]: t for t in self.schema.get("tables", [])
        }
        self.columns: dict[str, set[str]] = {
            name: {c["name"] for c in table.get("columns", [])}
            for name, table in self.tables.items()
        }
        self.column_owner: dict[str, list[str]] = {}
        for table, cols in self.columns.items():
            for col in cols:
                self.column_owner.setdefault(col, []).append(table)

    @staticmethod
    def _q(identifier: str) -> str:
        # Identifiers are only quoted after checking against metadata.
        if not identifier.replace("_", "").isalnum():
            raise SQLGenerationError(f"Invalid SQL identifier: {identifier!r}")
        return '"' + identifier.replace('"', '""') + '"'

    def _validate_table(self, table: str) -> None:
        if table not in self.tables:
            raise SQLGenerationError(f"Unknown table in schema metadata: {table}")

    def _resolve_column(self, column: str, tables: list[str], explicit_table: str | None = None) -> str:
        # Supports expressions only from a small, explicit allowlist.
        if column == "*":
            return "*"
        if column in {"quantity * unit_price", "SUM(quantity * unit_price)"}:
            return '( "invoice_items"."quantity" * "invoice_items"."unit_price" )'
        if column.startswith("month(") and column.endswith(")"):
            inner = column[6:-1]
            return f"EXTRACT(MONTH FROM {self._resolve_column(inner, tables)})"
        if column.startswith("year(") and column.endswith(")"):
            inner = column[5:-1]
            return f"EXTRACT(YEAR FROM {self._resolve_column(inner, tables)})"

        if "." in column:
            table, col = column.split(".", 1)
            self._validate_table(table)
            if col not in self.columns[table]:
                raise SQLGenerationError(f"Unknown column: {table}.{col}")
            return f'{self._q(table)}.{self._q(col)}'

        if explicit_table:
            self._validate_table(explicit_table)
            if column not in self.columns[explicit_table]:
                raise SQLGenerationError(f"Unknown column: {explicit_table}.{column}")
            return f'{self._q(explicit_table)}.{self._q(column)}'

        owners = [t for t in tables if column in self.columns.get(t, set())]
        if len(owners) == 1:
            return f'{self._q(owners[0])}.{self._q(column)}'
        if len(owners) > 1:
            # Prefer the first target table only if it actually owns the column;
            # otherwise require qualification to avoid ambiguous SQL.
            raise SQLGenerationError(
                f"Ambiguous column {column!r}; specify its table in the intent."
            )
        global_owners = self.column_owner.get(column, [])
        if len(global_owners) == 1:
            owner = global_owners[0]
            if owner not in tables:
                raise SQLGenerationError(
                    f"Column {column!r} belongs to {owner}, which is not in the target tables."
                )
            return f'{self._q(owner)}.{self._q(column)}'
        raise SQLGenerationError(f"Unknown or ambiguous column: {column}")

    def _from_clause(self, tables: list[str]) -> str:
        if not tables:
            raise SQLGenerationError("No target table was identified.")
        for table in tables:
            self._validate_table(table)
        ordered = list(dict.fromkeys(tables))
        sql = f'FROM {self._q(ordered[0])}'
        included = {ordered[0]}
        for table in ordered[1:]:
            join_condition = None
            for existing in included:
                join_condition = JOIN_SQL.get(frozenset({existing, table}))
                if join_condition:
                    break
            if join_condition:
                sql += f' JOIN {self._q(table)} ON {join_condition}'
                included.add(table)
            else:
                # Never silently create a Cartesian product.
                raise SQLGenerationError(
                    f"No approved join path found between {sorted(included)} and {table}."
                )
        return sql

    def _where(self, constraints: list[dict[str, Any]], tables: list[str]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        allowed_ops = {"=", "!=", "<>", ">", ">=", "<", "<=", "ILIKE", "LIKE"}
        for c in constraints:
            table = c.get("table")
            column = str(c.get("column", ""))
            operator = str(c.get("operator", "=")).upper()
            value = c.get("value")
            if operator == "BETWEEN":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise SQLGenerationError("BETWEEN requires exactly two values.")
                col_sql = self._resolve_column(column, tables, table)
                clauses.append(f"{col_sql} BETWEEN %s AND %s")
                params.extend(value)
            elif operator == "YEAR":
                col_sql = self._resolve_column(column, tables, table)
                clauses.append(f"EXTRACT(YEAR FROM {col_sql}) = %s")
                params.append(value)
            elif operator in {"LAST_MONTH", "THIS_MONTH", "LAST_YEAR", "THIS_YEAR"}:
                col_sql = self._resolve_column(column, tables, table)
                if operator == "LAST_MONTH":
                    clauses.append(f"{col_sql} >= date_trunc('month', CURRENT_DATE - INTERVAL '1 month') AND {col_sql} < date_trunc('month', CURRENT_DATE)")
                elif operator == "THIS_MONTH":
                    clauses.append(f"{col_sql} >= date_trunc('month', CURRENT_DATE) AND {col_sql} < date_trunc('month', CURRENT_DATE + INTERVAL '1 month')")
                elif operator == "LAST_YEAR":
                    clauses.append(f"{col_sql} >= date_trunc('year', CURRENT_DATE - INTERVAL '1 year') AND {col_sql} < date_trunc('year', CURRENT_DATE)")
                else:
                    clauses.append(f"{col_sql} >= date_trunc('year', CURRENT_DATE) AND {col_sql} < date_trunc('year', CURRENT_DATE + INTERVAL '1 year')")
            elif operator in allowed_ops:
                col_sql = self._resolve_column(column, tables, table)
                clauses.append(f"{col_sql} {operator} %s")
                params.append(value)
            else:
                raise SQLGenerationError(f"Unsupported constraint operator: {operator}")
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    def _select(self, intent: Any) -> tuple[str, list[Any], list[str]]:
        tables = list(dict.fromkeys(intent.target_tables))
        if not tables:
            raise SQLGenerationError("No target table identified; clarify the request.")
        aggregation = intent.aggregation or {}
        group_by = aggregation.get("group_by", [])
        params: list[Any] = []

        if aggregation.get("function") in {"count", "sum", "avg"}:
            fn = aggregation["function"].upper()
            measure = str(aggregation.get("measure", "*"))
            if fn == "COUNT" and measure == "*":
                projection = "COUNT(*) AS \"count\""
            elif measure == "quantity * unit_price" and fn == "SUM":
                projection = 'SUM("invoice_items"."quantity" * "invoice_items"."unit_price") AS "sum"'
            else:
                measure_sql = self._resolve_column(measure, tables)
                projection = f'{fn}({measure_sql}) AS "{fn.lower()}"'
            if group_by:
                group_sql = [self._resolve_column(str(g), tables) for g in group_by]
                projection = ", ".join(group_sql + [projection])
            sql = "SELECT " + projection + " " + self._from_clause(tables)
            where_sql, where_params = self._where(intent.constraints, tables)
            sql += where_sql
            params.extend(where_params)
            if group_by:
                sql += " GROUP BY " + ", ".join(self._resolve_column(str(g), tables) for g in group_by)
        else:
            requested = list(intent.target_columns)
            # If parser didn't infer columns, use all metadata columns for target tables.
            if not requested:
                requested = [c for t in tables for c in self.columns[t]]
            projections = [self._resolve_column(c, tables) for c in dict.fromkeys(requested)]
            sql = "SELECT " + ", ".join(projections) + " " + self._from_clause(tables)
            where_sql, where_params = self._where(intent.constraints, tables)
            sql += where_sql
            params.extend(where_params)

        if intent.order_by:
            order_terms = []
            for item in intent.order_by:
                col = str(item.get("column", ""))
                direction = str(item.get("direction", "asc")).upper()
                if direction not in {"ASC", "DESC"}:
                    raise SQLGenerationError("ORDER BY direction must be ASC or DESC.")
                # "revenue" is supported only for line-item results.
                if col == "revenue":
                    col_sql = '("invoice_items"."quantity" * "invoice_items"."unit_price")'
                else:
                    col_sql = self._resolve_column(col, tables)
                order_terms.append(f"{col_sql} {direction}")
            sql += " ORDER BY " + ", ".join(order_terms)

        limit = intent.limit
        if aggregation.get("function") == "top_n":
            limit = aggregation.get("limit", limit)
        if limit is None:
            limit = 100
        if not isinstance(limit, int) or limit < 1:
            raise SQLGenerationError("LIMIT must be a positive integer.")
        sql += " LIMIT %s"
        params.append(min(limit, 500))
        return sql, params, tables

    def generate(self, intent: Any) -> SQLAction:
        action_type = str(getattr(intent, "action_type", "unknown"))
        intent_name = str(getattr(intent, "intent", "unknown"))
        raw_text = str(getattr(intent, "raw_text", ""))
        confidence = float(getattr(intent, "confidence", 0.0))
        tables = list(dict.fromkeys(getattr(intent, "target_tables", []) or []))
        columns = list(dict.fromkeys(getattr(intent, "target_columns", []) or []))

        if action_type == "read":
            try:
                sql, params, tables = self._select(intent)
                return SQLAction("read", intent_name, sql, params, tables, columns,
                                 False, True, "Read-only SQL generated; not executed.",
                                 raw_text, confidence)
            except SQLGenerationError as exc:
                return SQLAction(action_type, intent_name, None, [], tables, columns,
                                 False, False, str(exc), raw_text, confidence)

        if action_type == "write":
            # Week 4 produces a write plan but Week 5 must approve it; no execution here.
            if intent_name == "update_record":
                if not getattr(intent, "set_values", None):
                    msg = "Cannot generate UPDATE: no validated set_values were parsed."
                elif not getattr(intent, "constraints", None):
                    msg = "UPDATE requires a WHERE constraint; unscoped update refused."
                else:
                    msg = "Write action detected. UPDATE generation is intentionally deferred until a table-specific update mapping is verified."
            elif intent_name in {"delete_record", "cancel_invoice", "insert_record"}:
                msg = "Write action detected. DML generation is intentionally deferred; Week 5 approval and Week 6 execution are required."
            else:
                msg = f"Unsupported write intent: {intent_name}"
            return SQLAction("write", intent_name, None, [], tables, columns,
                             True, False, msg, raw_text, confidence)

        if action_type == "ddl":
            return SQLAction("ddl", intent_name, None, [], tables, columns,
                             True, False, "DDL is not generated or executed in Weeks 4–5.", raw_text, confidence)

        return SQLAction(action_type, intent_name, None, [], tables, columns,
                         False, False, "Unknown action; clarify the natural-language request.",
                         raw_text, confidence)


def main() -> int:
    parser = argparse.ArgumentParser(description="Week 4 PostgreSQL SQL/action generator (generation only)")
    parser.add_argument("text", help="Natural-language request")
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    args = parser.parse_args()
    try:
        intent_parser = IntentParser.from_paths(metadata_path=args.metadata)
        parsed = intent_parser.parse(args.text)
        action = SQLAgent(args.metadata).generate(parsed)
        print(json.dumps(asdict(action), indent=2, default=str))
        return 0 if action.generated or action.action_type in {"write", "ddl"} else 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
