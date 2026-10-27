"""Generate and validate SQL plans without executing them."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

try:
    from .intent_parser import IntentParser, ParsedIntent
    from .llm_client import LLMClient, LLMUnavailableError, create_llm_client
    from .prompt_builder import PromptBuilder
    from .sql_validator import SQLValidator
except ImportError:
    from intent_parser import IntentParser, ParsedIntent
    from llm_client import LLMClient, LLMUnavailableError, create_llm_client
    from prompt_builder import PromptBuilder
    from sql_validator import SQLValidator


@dataclass
class ActionPlan:
    action_type: Literal["read", "write", "ddl"]
    sql: str
    tables: list[str]
    columns: list[str]
    explanation: str
    confidence: float
    count_sql: str | None
    blocked: bool
    block_reason: str | None


class SQLGenerator:
    def __init__(
        self,
        llm_client: LLMClient | None = None,
        metadata_path: Path | str | None = None,
        examples_path: Path | str | None = None,
        explain: bool = False,
    ) -> None:
        self.metadata_path = Path(metadata_path) if metadata_path else Path(__file__).with_name("schema_metadata.json")
        self.intent_parser = IntentParser.from_paths(
            metadata_path=self.metadata_path,
        )
        self.prompt_builder = PromptBuilder(
            metadata_path=self.metadata_path,
            examples_path=examples_path,
        )
        metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.validator = SQLValidator(metadata)
        self.llm_client = llm_client
        self.explain = explain

    @staticmethod
    def _blocked(parsed: ParsedIntent, action_type: str, reason: str) -> ActionPlan:
        return ActionPlan(
            action_type=action_type,
            sql="",
            tables=list(parsed.target_tables),
            columns=list(parsed.target_columns),
            explanation=reason,
            confidence=0.0,
            count_sql=None,
            blocked=True,
            block_reason=reason,
        )

    def _validate_response(self, response: Any, parsed: ParsedIntent) -> ActionPlan:
        if not isinstance(response, dict):
            raise ValueError("LLM output must be a JSON object.")
        required = {
            "action_type",
            "sql",
            "tables",
            "columns",
            "explanation",
            "confidence",
            "count_sql",
            "blocked",
            "block_reason",
        }
        missing = required - response.keys()
        if missing:
            raise ValueError(f"LLM output is missing required fields: {', '.join(sorted(missing))}.")

        action_type = response["action_type"]
        sql = response["sql"]
        tables = response["tables"]
        columns = response["columns"]
        explanation = response["explanation"]
        confidence = response["confidence"]
        count_sql = response["count_sql"]
        blocked = response["blocked"]
        block_reason = response["block_reason"]

        if action_type not in {"read", "write"} or action_type != parsed.action_type:
            raise ValueError("LLM action_type does not match the parsed intent.")
        if not isinstance(sql, str) or not isinstance(tables, list) or not all(isinstance(item, str) for item in tables):
            raise ValueError("LLM sql and tables fields have invalid types.")
        if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
            raise ValueError("LLM columns field must be a list of strings.")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("LLM explanation must be a non-empty string.")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("LLM confidence must be a number between 0 and 1.")
        if not isinstance(blocked, bool):
            raise ValueError("LLM blocked field must be a boolean.")
        if block_reason is not None and not isinstance(block_reason, str):
            raise ValueError("LLM block_reason must be a string or null.")
        if count_sql is not None and not isinstance(count_sql, str):
            raise ValueError("LLM count_sql must be a string or null.")

        if blocked:
            reason = block_reason or "The LLM marked this request as blocked."
            return self._blocked(parsed, action_type, reason)
        if block_reason is not None:
            raise ValueError("A non-blocked plan cannot include block_reason.")
        if not sql.strip():
            raise ValueError("A non-blocked plan must include SQL.")

        result = self.validator.validate(sql, action_type, explain=self.explain)
        if not result.valid:
            raise ValueError("; ".join(result.errors))

        if action_type == "write":
            if not count_sql:
                raise ValueError("Write plans must include count_sql.")
            count_result = self.validator.validate_count_sql(count_sql, sql)
            if not count_result.valid:
                raise ValueError("; ".join(count_result.errors))
        elif count_sql is not None:
            raise ValueError("Read plans must set count_sql to null.")

        return ActionPlan(
            action_type=action_type,
            sql=sql.strip(),
            tables=result.tables,
            columns=result.columns,
            explanation=explanation.strip(),
            confidence=float(confidence),
            count_sql=count_sql.strip() if count_sql else None,
            blocked=False,
            block_reason=None,
        )

    def _retry_feedback(self, response: Any, parsed: ParsedIntent, error: str) -> str:
        relevant_tables = set(parsed.target_tables)
        if isinstance(response, dict) and isinstance(response.get("sql"), str):
            try:
                statements = [
                    statement
                    for statement in sqlglot.parse(response["sql"], read="postgres")
                    if statement is not None
                ]
                for statement in statements:
                    relevant_tables.update(
                        table.name for table in statement.find_all(exp.Table)
                    )
            except ParseError:
                pass
        valid_columns = {
            table: sorted(self.validator.tables.get(table.casefold(), set()))
            for table in sorted(relevant_tables)
            if table.casefold() in self.validator.tables
        }
        return (
            "\n\nVALIDATION FEEDBACK (one correction attempt only):\n"
            f"{error}\n"
            "Regenerate the complete action plan. Use only these valid physical columns "
            f"for the relevant tables: {json.dumps(valid_columns)}. "
            "Do not treat SELECT aliases as physical columns."
        )

    def generate(self, text: str, session_id: str | None = None) -> ActionPlan:
        parsed = self.intent_parser.parse(text, session_id=session_id)
        if parsed.action_type == "ddl":
            return self._blocked(parsed, "ddl", "DDL is blocked; this week does not generate or execute DDL.")
        if parsed.action_type == "unknown":
            return self._blocked(parsed, "read", "The intent parser could not classify this request.")
        if parsed.action_type == "write" and parsed.is_unscoped:
            return self._blocked(parsed, "write", "Unscoped writes are blocked; provide a restrictive filter.")

        try:
            system, user, tool_schema = self.prompt_builder.build(parsed, text)
        except (TypeError, ValueError, KeyError) as error:
            return self._blocked(parsed, parsed.action_type, f"Invalid generated action plan: {error}")
        try:
            client = self.llm_client or create_llm_client()
            self.llm_client = client
        except (TypeError, ValueError) as error:
            return self._blocked(
                parsed,
                parsed.action_type,
                f"LLM unavailable/timeout: {type(error).__name__}: {error}",
            )

        retry_user = user
        for attempt in range(2):
            response: Any = None
            try:
                response = client.complete(
                    system,
                    retry_user,
                    tool_schema,
                    context=f"request={text!r}; attempt={attempt + 1}",
                )
                return self._validate_response(response, parsed)
            except LLMUnavailableError as error:
                reason = str(error).replace("\n", " ").strip()
                return self._blocked(
                    parsed,
                    parsed.action_type,
                    f"LLM unavailable/timeout: {reason}",
                )
            except (TypeError, ValueError, KeyError) as error:
                last_error = str(error)
                if attempt == 1:
                    return self._blocked(
                        parsed,
                        parsed.action_type,
                        f"Invalid generated action plan: {last_error}",
                    )
                retry_user += self._retry_feedback(response, parsed, last_error)

        return self._blocked(parsed, parsed.action_type, "SQL validation failed after one correction attempt.")


def _print_plan(plan: ActionPlan) -> None:
    print(json.dumps(asdict(plan), indent=2))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a validated PostgreSQL action plan (no execution).")
    parser.add_argument("text", nargs="?", help="Natural-language database request")
    parser.add_argument("--session-id", default="", help="Session identifier for follow-up context")
    parser.add_argument("--interactive", action="store_true", help="Start a session-aware REPL")
    parser.add_argument("--explain", action="store_true", help="Run plain EXPLAIN when PostgreSQL is configured")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.interactive and not args.text:
        parser.error("text is required unless --interactive is used")

    generator = SQLGenerator(explain=args.explain)
    session_id = args.session_id or ("interactive" if args.interactive else None)
    if args.interactive:
        while True:
            try:
                text = input("sql-plan> ").strip()
            except EOFError:
                break
            if not text or text.lower() in {"exit", "quit"}:
                break
            _print_plan(generator.generate(text, session_id=session_id))
        return 0

    try:
        _print_plan(generator.generate(args.text or "", session_id=session_id))
    except ValueError as error:
        print(f"SQL generation unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
