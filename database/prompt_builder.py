"""Prompt construction from parsed intent, retrieved schema, and SQL examples."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    from .intent_parser import ParsedIntent
    from .schema_retrieval import SchemaRetrievalEngine, build_engine
except ImportError:
    from intent_parser import ParsedIntent
    from schema_retrieval import SchemaRetrievalEngine, build_engine


READ_SYSTEM_PROMPT = """You generate one safe PostgreSQL SELECT statement from a user's request.
Return only the requested function call. Use ONLY the tables and columns listed below.
If the request cannot be answered with them, return a blocked plan with a reason; never
invent identifiers. Qualify columns when joins could make them ambiguous. Every SELECT
must have a positive LIMIT of at most 100. Never generate a write, DDL, multiple
statements, or a query that changes data. Use supplied parsed constraints and session
context; do not invent filter values."""

WRITE_SYSTEM_PROMPT = """You generate one PostgreSQL INSERT, UPDATE, or DELETE statement from
a user's request. Return only the requested function call. Use ONLY the tables and columns
listed below. If the request cannot be answered with them, return a blocked plan with a
reason; never invent identifiers. UPDATE and DELETE must have a restrictive WHERE clause;
never use an unscoped write. For UPDATE and DELETE, provide a SELECT COUNT(*) with the
same target table and WHERE condition as the write. For INSERT, provide a SELECT COUNT(*)
query against the target table scoped to the inserted row's identifying values. Never
generate DDL or multiple statements. The SQL is executed only after guardrail approval,
inside a sandboxed transaction that is rolled back if any pre-commit check fails."""

TOKEN_RE = re.compile(r"[a-z0-9]+")


class PromptBuilder:
    def __init__(
        self,
        metadata_path: Path | str | None = None,
        examples_path: Path | str | None = None,
        retrieval_engine: SchemaRetrievalEngine | None = None,
        top_k: int = 3,
        example_count: int = 3,
    ) -> None:
        self.metadata_path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        examples_file = Path(examples_path) if examples_path else Path(__file__).with_name("few_shot_examples.json")
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.examples = json.loads(examples_file.read_text(encoding="utf-8"))
        self.retrieval_engine = retrieval_engine or build_engine(self.metadata_path)
        self.top_k = top_k
        self.example_count = example_count

    def _best_examples(self, text: str, action_type: str) -> list[dict[str, Any]]:
        query_terms = set(TOKEN_RE.findall(text.lower()))
        scored: list[tuple[float, dict[str, Any]]] = []
        for example in self.examples:
            if example.get("action_type") != action_type:
                continue
            terms = set(TOKEN_RE.findall(str(example.get("question", "")).lower()))
            score = len(query_terms & terms) / max(len(query_terms), 1)
            if score:
                scored.append((score, example))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("question", ""))))
        return [example for _, example in scored[: self.example_count]]

    def build(self, parsed: ParsedIntent, user_text: str) -> tuple[str, str, dict[str, Any]]:
        action_type = parsed.action_type
        if action_type not in {"read", "write"}:
            raise ValueError(f"Cannot build an LLM prompt for action type {action_type!r}.")

        retrieved = self.retrieval_engine.query(user_text, top_k=self.top_k)
        relevant_names = {str(result["name"]) for result in retrieved}
        schema_slice = [result["metadata"] for result in retrieved]
        table_names = {str(table.get("name", "")) for table in schema_slice}
        for table_name in parsed.target_tables:
            if table_name not in table_names:
                table = next(
                    (item for item in self.metadata.get("tables", []) if item.get("name") == table_name),
                    None,
                )
                if table is not None:
                    schema_slice.append(table)
                    table_names.add(table_name)
                    relevant_names.add(table_name)
        relevant_names.update(parsed.target_tables)

        table_metadata = {
            str(table["name"]): table
            for table in self.metadata.get("tables", [])
        }
        join_paths: set[str] = set()
        for table in table_metadata.values():
            for foreign_key in table.get("foreign_keys", []):
                reference = re.fullmatch(
                    r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*",
                    str(foreign_key.get("references", "")),
                )
                if not reference:
                    continue
                referenced_table, referenced_column = reference.groups()
                for column in foreign_key.get("columns", []):
                    join_paths.add(f"{table['name']}.{column} -> {referenced_table}.{referenced_column}")
                if table["name"] in relevant_names:
                    relevant_names.add(referenced_table)
                if referenced_table in relevant_names:
                    relevant_names.add(str(table["name"]))

        # Include one-hop FK neighbors so the model can form schema-supported joins.
        for table_name in sorted(relevant_names):
            table = table_metadata.get(table_name)
            if table is not None and table not in schema_slice:
                schema_slice.append(table)

        grounded_schema = []
        for table in schema_slice:
            table_name = str(table["name"])
            primary_keys = set(table.get("primary_key", []))
            foreign_key_columns = {
                str(column)
                for foreign_key in table.get("foreign_keys", [])
                for column in foreign_key.get("columns", [])
            }
            grounded_schema.append(
                {
                    "name": table_name,
                    "description": table.get("description", ""),
                    "columns": [
                        {
                            "name": column["name"],
                            "type": column.get("type"),
                            "markers": [
                                marker
                                for marker, active in (
                                    ("PRIMARY KEY", column["name"] in primary_keys),
                                    ("FOREIGN KEY", column["name"] in foreign_key_columns),
                                )
                                if active
                            ],
                        }
                        for column in table.get("columns", [])
                    ],
                }
            )

        system = READ_SYSTEM_PROMPT if action_type == "read" else WRITE_SYSTEM_PROMPT
        user = json.dumps(
            {
                "request": user_text,
                "parsed_intent": asdict(parsed),
                "session_context": parsed.session_context,
                "retrieved_tables": [
                    {"name": result["name"], "score": result["score"]}
                    for result in retrieved
                ],
                "schema": grounded_schema,
                "primary_join_paths": sorted(join_paths),
                "few_shot_examples": self._best_examples(user_text, action_type),
            },
            ensure_ascii=False,
        )
        tool_schema = {
            "name": "provide_action_plan",
            "description": "Return a validated PostgreSQL action plan; do not execute it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {"type": "string", "enum": ["read", "write"]},
                    "sql": {"type": "string"},
                    "tables": {"type": "array", "items": {"type": "string"}},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "count_sql": {"type": ["string", "null"]},
                    "blocked": {"type": "boolean"},
                    "block_reason": {"type": ["string", "null"]},
                },
                "required": [
                    "action_type",
                    "sql",
                    "tables",
                    "columns",
                    "explanation",
                    "confidence",
                    "count_sql",
                    "blocked",
                    "block_reason",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        }
        return system, user, tool_schema
