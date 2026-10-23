"""Print diagnostics for one or more SQL-generation requests without executing SQL."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

try:
    from .intent_parser import IntentParser
    from .llm_client import MockLLMClient, create_llm_client
    from .prompt_builder import PromptBuilder
    from .sql_generator import SQLGenerator
    from .sql_validator import SQLValidator
except ImportError:
    from intent_parser import IntentParser
    from llm_client import MockLLMClient, create_llm_client
    from prompt_builder import PromptBuilder
    from sql_generator import SQLGenerator
    from sql_validator import SQLValidator


DEFAULT_REQUESTS = [
    "how many invoices per country",
    "revenue per country",
    "top 5 customers by number of invoices",
    "show customers from France",
]


def _sql_schema_columns(schema_sql: str) -> dict[str, list[str]]:
    tables: dict[str, list[str]] = {}
    table_pattern = re.compile(
        r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\);",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in table_pattern.finditer(schema_sql):
        table_name, definition = match.groups()
        columns = []
        for line in definition.splitlines():
            if not line.startswith("    ") or line.startswith("        "):
                continue
            line = line.strip().rstrip(",")
            column_match = re.match(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s+', line)
            if column_match and not column_match.group(1).upper() in {
                "CONSTRAINT",
                "PRIMARY",
                "FOREIGN",
                "UNIQUE",
                "CHECK",
            }:
                columns.append(column_match.group(1))
        tables[table_name] = columns
    return tables


def _sql_identifiers(sql: str) -> dict[str, list[str]]:
    parsed = [statement for statement in sqlglot.parse(sql, read="postgres") if statement is not None]
    tables: list[str] = []
    columns: list[str] = []
    aliases: list[str] = []
    for statement in parsed:
        for table in statement.find_all(exp.Table):
            if table.name not in tables:
                tables.append(table.name)
        for column in statement.find_all(exp.Column):
            if column.name not in columns:
                columns.append(column.name)
        for alias in statement.find_all(exp.Alias):
            if alias.alias not in aliases:
                aliases.append(alias.alias)
    return {"tables": tables, "columns": columns, "select_aliases": aliases}


def diagnose_request(
    request: str,
    parser: IntentParser,
    prompt_builder: PromptBuilder,
    validator: SQLValidator,
) -> None:
    print(f"\n{'=' * 88}\nREQUEST: {request}\n{'=' * 88}")
    parsed = parser.parse(request)
    print("\n(1) PARSED INTENT")
    print(json.dumps(asdict(parsed), indent=2))

    retrieved = prompt_builder.retrieval_engine.query(request, top_k=prompt_builder.top_k)
    print("\n(2) SCHEMA RETRIEVAL (table names and exact columns)")
    for result in retrieved:
        metadata = result["metadata"]
        columns = [
            {"name": column["name"], "type": column.get("type")}
            for column in metadata.get("columns", [])
        ]
        print(json.dumps({"name": result["name"], "score": result["score"], "columns": columns}, indent=2))

    system, user, tool_schema = prompt_builder.build(parsed, request)
    print("\n(3) EXACT SYSTEM PROMPT")
    print(system)
    print("\n(3) EXACT USER PROMPT")
    print(user)
    print("\n(3) TOOL SCHEMA")
    print(json.dumps(tool_schema, indent=2))

    response: dict[str, Any] | None = None
    try:
        response = create_llm_client().complete(system, user, tool_schema)
        print("\n(4) RAW LLM RESPONSE (before validation)")
        print(json.dumps(response, indent=2))
    except Exception as error:
        print(f"\n(4) RAW LLM RESPONSE unavailable: {type(error).__name__}: {error}")

    schema_path = Path(__file__).with_name("schema_metadata.json")
    schema_metadata = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_columns = {
        table["name"]: {column["name"] for column in table.get("columns", [])}
        for table in schema_metadata.get("tables", [])
    }
    schema_sql = _sql_schema_columns(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    sql = response.get("sql", "") if response else ""
    identifier_result: dict[str, list[str]] = {}
    if sql:
        try:
            identifier_result = _sql_identifiers(sql)
            print("\n(5) SQLGLOT IDENTIFIERS (from generated SQL)")
            print(json.dumps(identifier_result, indent=2))
        except sqlglot.errors.ParseError as error:
            print(f"\n(5) SQLGLOT parse failed: {error}")

    tables_involved = set(parsed.target_tables)
    tables_involved.update(result["name"] for result in retrieved)
    tables_involved.update(identifier_result.get("tables", []))
    print("\n(6) SCHEMA COLUMNS BY TABLE (metadata vs schema.sql)")
    for table in sorted(tables_involved):
        metadata_columns = sorted(schema_columns.get(table, set()))
        ddl_columns = schema_sql.get(table, [])
        print(
            json.dumps(
                {
                    "table": table,
                    "schema_metadata.json": metadata_columns,
                    "schema.sql": ddl_columns,
                    "in_sync": set(metadata_columns) == set(ddl_columns),
                },
                indent=2,
            )
        )

    print("\n(7) IDENTIFIER VALIDATION DIAGNOSIS")
    if response is None:
        print("No raw SQL response was available, so generated-SQL validation cannot be diagnosed.")
        return
    for reported_column in response.get("columns", []):
        if not isinstance(reported_column, str):
            print(f"LLM-reported column {reported_column!r} is not a string.")
            continue
        if not any(
            reported_column.casefold() == column.casefold()
            for columns in schema_columns.values()
            for column in columns
        ):
            is_alias = reported_column.casefold() in {
                alias.casefold() for alias in identifier_result.get("select_aliases", [])
            }
            kind = "SQL output alias" if is_alias else "not present in physical schema"
            print(f"LLM-reported {reported_column!r}: {kind}; the hardened generator ignores this list.")
    if sql:
        result = validator.validate(sql, response.get("action_type", parsed.action_type))
        print(json.dumps({"valid": result.valid, "errors": result.errors}, indent=2))
        for identifier in identifier_result.get("columns", []):
            if identifier.casefold() in {
                "count",
                "sum",
                "avg",
                "min",
                "max",
                "extract",
            }:
                continue
            owners = [
                table
                for table, columns in schema_columns.items()
                if identifier.casefold() in {column.casefold() for column in columns}
            ]
            print(f"SQL identifier {identifier!r}: metadata owners={owners or 'none'}")
        try:
            plan = SQLGenerator(llm_client=MockLLMClient([]))._validate_response(response, parsed)
            print(
                "HARDENED GENERATOR RESULT: "
                + json.dumps(
                    {
                        "blocked": plan.blocked,
                        "tables": plan.tables,
                        "columns": plan.columns,
                        "block_reason": plan.block_reason,
                    },
                    indent=2,
                )
            )
        except (TypeError, ValueError, KeyError) as error:
            print(f"HARDENED GENERATOR VALIDATION FAILURE: {error}")
    else:
        print("The response contains no SQL to validate.")


def main(argv: Iterable[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("requests", nargs="*", help="Requests to inspect; defaults to four regression cases")
    args = arg_parser.parse_args(list(argv) if argv is not None else None)

    parser = IntentParser.from_paths()
    prompt_builder = PromptBuilder()
    validator = SQLValidator()
    for request in args.requests or DEFAULT_REQUESTS:
        diagnose_request(request, parser, prompt_builder, validator)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
