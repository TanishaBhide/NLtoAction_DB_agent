"""Static PostgreSQL SQL validation; statements are never executed."""

from __future__ import annotations

import os
from difflib import get_close_matches
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    explain_message: str | None = None


class SQLValidator:
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        if metadata is None:
            import json
            from pathlib import Path

            metadata = json.loads(
                Path(__file__).with_name("schema_metadata.json").read_text(encoding="utf-8")
            )
        self.table_names = {
            str(table["name"]).casefold(): str(table["name"])
            for table in metadata.get("tables", [])
        }
        self.tables: dict[str, set[str]] = {
            str(table["name"]).casefold(): {
                str(column["name"]).casefold() for column in table.get("columns", [])
            }
            for table in metadata.get("tables", [])
        }

    @staticmethod
    def _suggest(name: str, candidates: set[str]) -> str:
        matches = get_close_matches(name.casefold(), sorted(candidates), n=3, cutoff=0.55)
        return f"; did you mean: {', '.join(matches)}?" if matches else ""

    def _parse_one(self, sql: str) -> tuple[exp.Expression | None, list[str]]:
        if not isinstance(sql, str) or not sql.strip():
            return None, ["SQL must be a non-empty string."]
        try:
            statements = [statement for statement in sqlglot.parse(sql, read="postgres") if statement is not None]
        except ParseError as error:
            return None, [f"SQL parse error: {error}"]
        if len(statements) != 1:
            return None, ["SQL must contain exactly one statement."]
        return statements[0], []

    def _inspect_schema(self, statement: exp.Expression) -> tuple[list[str], list[str], list[str]]:
        errors: list[str] = []
        table_aliases: dict[str, tuple[str, str]] = {}
        referenced_tables: list[str] = []
        cte_names = {
            cte.alias_or_name.casefold()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }
        cte_columns: dict[str, set[str]] = {}
        for cte in statement.find_all(exp.CTE):
            cte_name = cte.alias_or_name.casefold()
            query = cte.this
            if isinstance(query, exp.Subquery):
                query = query.this
            outputs: set[str] = set()
            if isinstance(query, exp.Select):
                for projection in query.expressions:
                    if isinstance(projection, exp.Star):
                        continue
                    if isinstance(projection, exp.Alias):
                        outputs.add(projection.alias.casefold())
                    elif isinstance(projection, exp.Column):
                        outputs.add(projection.name.casefold())
            cte_columns[cte_name] = outputs

        for table in statement.find_all(exp.Table):
            name = table.name.casefold()
            if name in cte_names:
                table_aliases[name] = ("cte", name)
                if table.alias:
                    table_aliases[table.alias.casefold()] = ("cte", name)
                continue
            schema_name = table.db.casefold() if table.db else None
            if schema_name not in {None, "public"}:
                errors.append(f"Schema {table.db!r} is not allowed.")
            if name not in self.tables:
                errors.append(
                    f"table {table.name!r} not found in schema metadata"
                    f"{self._suggest(table.name, set(self.table_names))}"
                )
                continue
            canonical = self.table_names[name]
            if canonical not in referenced_tables:
                referenced_tables.append(canonical)
            table_aliases[name] = ("table", name)
            if table.alias:
                table_aliases[table.alias.casefold()] = ("table", name)

        def is_select_alias_reference(column: exp.Column, name: str) -> bool:
            if column.find_ancestor(exp.Order, exp.Group) is None:
                return False
            select = column.find_ancestor(exp.Select)
            if select is None:
                return False
            return any(
                isinstance(projection, exp.Alias)
                and projection.alias.casefold() == name
                for projection in select.expressions
            )

        referenced_columns: list[str] = []
        for column in statement.find_all(exp.Column):
            name = column.name.casefold()
            if name == "*":
                qualifier = column.table.casefold()
                if qualifier and qualifier not in table_aliases:
                    errors.append(
                        f"table or alias {column.table!r} for wildcard selection "
                        "was not found in the query"
                    )
                continue
            qualifier = column.table.casefold()
            if qualifier:
                if is_select_alias_reference(column, name):
                    continue
                relation = table_aliases.get(qualifier)
                if relation is None:
                    errors.append(
                        f"table or alias {column.table!r} for column {column.name!r} "
                        "was not found in the query"
                    )
                    continue
                relation_type, table_name = relation
                if relation_type == "cte":
                    if name not in cte_columns.get(table_name, set()):
                        candidates = cte_columns.get(table_name, set())
                        errors.append(
                            f"column {column.name!r} not found in CTE {table_name!r}"
                            f"{self._suggest(column.name, candidates)}"
                        )
                        continue
                    if column.name not in referenced_columns:
                        referenced_columns.append(column.name)
                    continue
                owners = [table_name] if name in self.tables[table_name] else []
                if not owners:
                    other_tables = [
                        self.table_names[table]
                        for table, columns in self.tables.items()
                        if name in columns
                    ]
                    elsewhere = (
                        f"; it exists on {', '.join(other_tables)}"
                        if other_tables
                        else ""
                    )
                    errors.append(
                        f"column {column.name!r} not found in table "
                        f"{self.table_names[table_name]!r}{elsewhere}"
                        f"{self._suggest(column.name, self.tables[table_name])}"
                    )
                    continue
            else:
                if is_select_alias_reference(column, name):
                    continue
                inside_cte = column.find_ancestor(exp.CTE) is not None
                owners = []
                for relation_type, table_name in set(table_aliases.values()):
                    if relation_type == "cte":
                        if not inside_cte and name in cte_columns.get(table_name, set()):
                            owners.append(("cte", table_name))
                    elif name in self.tables[table_name]:
                        owners.append(("table", table_name))
            if not owners:
                involved_tables = {
                    table_name
                    for relation_type, table_name in table_aliases.values()
                    if relation_type == "table"
                }
                candidates = set().union(*(self.tables[table] for table in involved_tables)) if involved_tables else set()
                other_tables = [
                    self.table_names[table]
                    for table, columns in self.tables.items()
                    if name in columns and table not in involved_tables
                ]
                elsewhere = f"; it exists on {', '.join(other_tables)}" if other_tables else ""
                table_context = ", ".join(
                    self.table_names[table] for table in sorted(involved_tables)
                ) or "the query"
                errors.append(
                    f"column {column.name!r} not found in table(s) {table_context}{elsewhere}"
                    f"{self._suggest(column.name, candidates)}"
                )
                continue
            if len(set(owners)) > 1:
                errors.append(f"Ambiguous unqualified column: {column.name}")
                continue
            if column.name not in referenced_columns:
                referenced_columns.append(column.name)

        if isinstance(statement, exp.Insert):
            target = statement.this
            if isinstance(target, exp.Schema):
                target_table = target.this
                if isinstance(target_table, exp.Table):
                    table_name = target_table.name.casefold()
                    if table_name in self.tables:
                        for target_column in target.expressions:
                            column_name = target_column.name.casefold()
                            if column_name not in self.tables[table_name]:
                                errors.append(
                                    f"column {target_column.name!r} not found in table "
                                    f"{self.table_names[table_name]}"
                                    f"{self._suggest(target_column.name, self.tables[table_name])}"
                                )
                            elif target_column.name not in referenced_columns:
                                referenced_columns.append(target_column.name)

        return referenced_tables, referenced_columns, errors

    @staticmethod
    def _action_matches(statement: exp.Expression, action_type: str) -> bool:
        if action_type == "read":
            return isinstance(statement, exp.Query)
        if action_type == "write":
            return isinstance(statement, (exp.Insert, exp.Update, exp.Delete))
        if action_type == "ddl":
            return isinstance(statement, (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable))
        return False

    def validate(
        self,
        sql: str,
        action_type: str,
        *,
        require_limit: bool = True,
        explain: bool = False,
    ) -> ValidationResult:
        statement, errors = self._parse_one(sql)
        if statement is None:
            return ValidationResult(valid=False, errors=errors)

        if not self._action_matches(statement, action_type):
            errors.append(f"Statement type does not match action_type {action_type!r}.")
        if action_type == "read":
            if any(
                isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter))
                for node in statement.walk()
            ):
                errors.append("Read statements cannot contain writes or DDL, including nested statements.")
            if require_limit and isinstance(statement, exp.Query):
                limit = statement.args.get("limit")
                if limit is None:
                    errors.append("Read statements must include LIMIT.")
                elif limit.expression is None or not isinstance(limit.expression, exp.Literal):
                    errors.append("Read LIMIT must be a positive integer literal.")
                elif not limit.expression.is_int or int(limit.expression.this) <= 0:
                    errors.append("Read LIMIT must be a positive integer.")
                elif int(limit.expression.this) > 100:
                    errors.append("Read LIMIT cannot exceed 100.")
        if isinstance(statement, (exp.Update, exp.Delete)) and statement.args.get("where") is None:
            errors.append("UPDATE and DELETE statements must include WHERE.")

        tables, columns, schema_errors = self._inspect_schema(statement)
        errors.extend(schema_errors)
        result = ValidationResult(
            valid=not errors,
            errors=errors,
            tables=tables,
            columns=columns,
        )
        if explain and result.valid:
            result.explain_message = self._explain(sql)
        return result

    def validate_count_sql(self, count_sql: str, write_sql: str) -> ValidationResult:
        result = self.validate(count_sql, "read", require_limit=False)
        if not result.valid:
            return result
        count_statement, errors = self._parse_one(count_sql)
        write_statement, write_errors = self._parse_one(write_sql)
        errors.extend(write_errors)
        if count_statement is None or write_statement is None:
            return ValidationResult(False, errors, result.tables, result.columns)

        counts_all = isinstance(count_statement, exp.Select) and any(
            isinstance(node, exp.Count) and isinstance(node.this, exp.Star)
            for projection in count_statement.expressions
            for node in projection.find_all(exp.Count)
        )
        if not counts_all:
            errors.append("count_sql must select COUNT(*).")

        if isinstance(write_statement, (exp.Update, exp.Delete)):
            if isinstance(write_statement, exp.Update) and write_statement.args.get("from_"):
                errors.append("UPDATE FROM is not supported by the count_sql guardrail.")
            if isinstance(write_statement, exp.Delete) and write_statement.args.get("using"):
                errors.append("DELETE USING is not supported by the count_sql guardrail.")
            target = write_statement.this
            count_from = count_statement.args.get("from_")
            count_table = count_from.this if count_from is not None else None
            if not isinstance(target, exp.Table) or not isinstance(count_table, exp.Table):
                errors.append("count_sql must use the same target table as the write.")
            elif target.name.casefold() != count_table.name.casefold():
                errors.append("count_sql must use the same target table as the write.")
            write_where = write_statement.args.get("where")
            count_where = count_statement.args.get("where")
            write_signature = write_where.this.sql(dialect="postgres", normalize=True) if write_where else None
            count_signature = count_where.this.sql(dialect="postgres", normalize=True) if count_where else None
            if write_signature != count_signature:
                errors.append("count_sql must use the same WHERE condition as the write.")
        elif isinstance(write_statement, exp.Insert):
            target = write_statement.this
            count_from = count_statement.args.get("from_")
            count_table = count_from.this if count_from is not None else None
            if isinstance(target, exp.Schema):
                target = target.this
            if not isinstance(target, exp.Table) or not isinstance(count_table, exp.Table):
                errors.append("count_sql for INSERT must count rows in the target table.")
            elif target.name.casefold() != count_table.name.casefold():
                errors.append("count_sql for INSERT must count rows in the target table.")
        else:
            errors.append("count_sql is only supported for INSERT, UPDATE, or DELETE.")

        result.errors.extend(errors)
        result.valid = not result.errors
        return result

    @staticmethod
    def _explain(sql: str) -> str:
        database_url = os.environ.get("DATABASE_URL")
        connection_options: dict[str, str] = {}
        if not database_url:
            for variable, option in (
                ("PGHOST", "host"),
                ("PGPORT", "port"),
                ("PGDATABASE", "dbname"),
                ("PGUSER", "user"),
                ("PGPASSWORD", "password"),
            ):
                value = os.environ.get(variable)
                if value:
                    connection_options[option] = value
            if not connection_options.get("dbname"):
                return "EXPLAIN skipped: database connection is not configured."
        try:
            import psycopg2
        except ImportError:
            return "EXPLAIN skipped: psycopg2 is not installed."
        try:
            connection = psycopg2.connect(database_url) if database_url else psycopg2.connect(**connection_options)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN " + sql)
                    cursor.fetchall()
            finally:
                connection.close()
        except psycopg2.Error as error:
            return f"EXPLAIN unavailable: {error}"
        return "EXPLAIN completed; the statement was not executed."
