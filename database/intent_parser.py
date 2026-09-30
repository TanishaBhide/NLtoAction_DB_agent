"""Week 3 natural-language intent parser.

This parser stays standard-library only and turns a user request into a
structured action plan that later weeks can consume for SQL generation,
guardrails, and audit logging.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from schema_retrieval import SchemaRetrievalEngine, build_engine


DDL_KEYWORDS = {"drop", "truncate", "alter", "grant", "revoke", "create table"}
READ_KEYWORDS = {"show", "list", "find", "get", "select", "display", "retrieve", "count", "how many", "what", "which"}
WRITE_KEYWORDS = {"insert", "add", "create", "update", "delete", "remove", "cancel", "commit", "save"}

DESTRUCTIVE_SYNONYMS = {
    "delete": "delete",
    "remove": "delete",
    "get rid of": "delete",
    "wipe": "truncate",
    "erase": "delete",
    "purge": "delete",
    "clear out": "delete",
    "drop": "drop",
    "truncate": "truncate",
}

READ_ACTION_STARTERS = {"show", "list", "find", "get", "select", "display", "retrieve", "count", "what", "which"}
PAST_TENSE_TOKENS = {"deleted", "updated", "inserted", "created", "removed", "cancelled", "truncated"}

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized = _normalize(text)
    phrase = _normalize(phrase)
    if not phrase:
        return False
    if " " in phrase:
        return phrase in normalized
    return phrase in _words(normalized)


def _extract_quoted(text: str) -> list[str]:
    values: list[str] = []
    for match in QUOTED_RE.finditer(text):
        value = match.group(1) or match.group(2)
        if value:
            values.append(value)
    return values


def _to_number(value: str) -> int | float | str:
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    try:
        return float(value)
    except ValueError:
        return value


def _token_score(left: str, right: str) -> float:
    left_tokens = set(_words(left))
    right_tokens = set(_words(right))
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / max(len(right_tokens), 1)


@dataclass(frozen=True)
class ParsedIntent:
    raw_text: str
    action_type: str
    intent: str
    target_tables: list[str] = field(default_factory=list)
    target_columns: list[str] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    set_values: dict[str, Any] = field(default_factory=dict)
    aggregation: dict[str, Any] | None = None
    order_by: list[dict[str, Any]] = field(default_factory=list)
    limit: int | None = None
    entities: dict[str, Any] = field(default_factory=dict)
    session_context: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    ambiguities: list[str] = field(default_factory=list)
    is_destructive: bool = False
    is_unscoped: bool = False
    resolved_from_context: bool = False
    previous_request: str | None = None
    matched_examples: list[dict[str, Any]] = field(default_factory=list)


class SessionStore:
    def __init__(self) -> None:
        self._store: dict[str, ParsedIntent] = {}

    def get(self, session_id: str) -> ParsedIntent | None:
        return self._store.get(session_id)

    def put(self, session_id: str, parsed_intent: ParsedIntent) -> None:
        self._store[session_id] = parsed_intent


class IntentParser:
    def __init__(self, engine: SchemaRetrievalEngine, examples_path: Path | None = None) -> None:
        self.engine = engine
        self.schema = self._load_schema_metadata()
        self.examples = self._load_examples(examples_path)
        self.session_store = SessionStore()
        self.table_names = [str(table["name"]) for table in self.schema.get("tables", [])]
        self.column_to_table = self._build_column_map()
        self.country_values = self._build_country_values()

    @classmethod
    def from_paths(
        cls,
        metadata_path: Path | None = None,
        examples_path: Path | None = None,
    ) -> "IntentParser":
        return cls(engine=build_engine(metadata_path), examples_path=examples_path)

    def _load_schema_metadata(self) -> dict[str, Any]:
        path = Path(__file__).with_name("schema_metadata.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_examples(self, examples_path: Path | None) -> list[dict[str, Any]]:
        path = examples_path or Path(__file__).with_name("intent_examples.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _build_column_map(self) -> dict[str, str]:
        column_map: dict[str, str] = {}
        for table in self.schema.get("tables", []):
            table_name = str(table.get("name", ""))
            for column in table.get("columns", []):
                column_name = str(column.get("name", ""))
                if column_name:
                    column_map[column_name] = table_name
        return column_map

    def _build_country_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for table in self.schema.get("tables", []):
            if table.get("name") != "customers":
                continue
            for column in table.get("columns", []):
                if column.get("name") == "country":
                    for value in column.get("known_values", []):
                        values[str(value).lower()] = str(value)
        return values

    def _table_matches(self, text: str, top_k: int = 3) -> list[str]:
        normalized = _normalize(text)
        exact: list[str] = []
        for table in self.schema.get("tables", []):
            table_name = str(table.get("name", ""))
            candidates = {table_name.lower(), table_name.replace("_", " ").lower()}
            candidates.update(str(keyword).lower() for keyword in table.get("keywords", []))
            if any(_contains_phrase(normalized, candidate) for candidate in candidates):
                exact.append(table_name)

        retrieved = [str(item["name"]) for item in self.engine.query(text, top_k=top_k * 2) if float(item["score"]) >= 0.2]
        ordered: list[str] = []
        for table_name in exact + retrieved:
            if table_name not in ordered:
                ordered.append(table_name)
        return ordered[:top_k]

    def _column_matches(self, text: str) -> list[str]:
        normalized = _normalize(text)
        matches: list[str] = []
        for table in self.schema.get("tables", []):
            for column in table.get("columns", []):
                column_name = str(column.get("name", ""))
                candidates = {column_name.lower(), column_name.replace("_", " ").lower()}
                candidates.update(str(keyword).lower() for keyword in column.get("keywords", []))
                if any(_contains_phrase(normalized, candidate) for candidate in candidates):
                    matches.append(column_name)
        return sorted(set(matches))

    def _infer_table_for_column(self, column_name: str) -> str | None:
        return self.column_to_table.get(column_name)

    def _add_constraint(self, constraints: list[dict[str, Any]], table: str | None, column: str, operator: str, value: Any) -> None:
        constraint = {"table": table, "column": column, "operator": operator, "value": value}
        if constraint not in constraints:
            constraints.append(constraint)

    def _replace_or_add_constraint(self, constraints: list[dict[str, Any]], new_constraint: dict[str, Any]) -> None:
        for index, existing in enumerate(constraints):
            if existing.get("table") == new_constraint.get("table") and existing.get("column") == new_constraint.get("column"):
                constraints[index] = new_constraint
                return
        constraints.append(new_constraint)

    def _match_known_values(self, text: str, constraints: list[dict[str, Any]], target_tables: list[str], target_columns: list[str]) -> None:
        normalized = _normalize(text)
        for lower_value, original_value in self.country_values.items():
            if _contains_phrase(normalized, lower_value):
                self._add_constraint(constraints, "customers", "country", "=", original_value)
                if "customers" not in target_tables:
                    target_tables.append("customers")
                if "country" not in target_columns:
                    target_columns.append("country")

    def _match_basic_equality(self, text: str, constraints: list[dict[str, Any]], target_tables: list[str], target_columns: list[str]) -> None:
        normalized = _normalize(text)

        invoice_match = re.search(r"\binvoice\s+([A-Z]?[0-9]{3,})\b", text, flags=re.IGNORECASE)
        if invoice_match:
            invoice_no = invoice_match.group(1).upper()
            if not invoice_no.startswith(("A", "C")) and len(invoice_no) >= 6:
                self._add_constraint(constraints, "invoices", "invoice_no", "=", invoice_no)
                if "invoices" not in target_tables:
                    target_tables.append("invoices")

        stock_match = re.search(r"\bstock\s*code\s+([A-Z0-9]+)\b", text, flags=re.IGNORECASE)
        if stock_match:
            stock_code = stock_match.group(1).upper()
            self._add_constraint(constraints, "products", "stock_code", "=", stock_code)
            if "products" not in target_tables:
                target_tables.append("products")
            if "stock_code" not in target_columns:
                target_columns.append("stock_code")

        customer_match = re.search(r"\bcustomer\s+([0-9]+(?:\.0)?)\b", text, flags=re.IGNORECASE)
        if customer_match:
            customer_id = customer_match.group(1)
            if customer_id.endswith(".0"):
                customer_id = customer_id[:-2]
            self._add_constraint(constraints, "customers", "customer_id", "=", customer_id)
            if "customers" not in target_tables:
                target_tables.append("customers")
            if "customer_id" not in target_columns:
                target_columns.append("customer_id")

        for country in self.country_values.values():
            if _contains_phrase(normalized, country):
                self._add_constraint(constraints, "customers", "country", "=", country)
                if "customers" not in target_tables:
                    target_tables.append("customers")
                if "country" not in target_columns:
                    target_columns.append("country")

        for table in self.schema.get("tables", []):
            table_name = str(table.get("name", ""))
            for column in table.get("columns", []):
                column_name = str(column.get("name", ""))
                if column_name in {"invoice_no", "stock_code", "country", "invoice_date", "unit_price", "quantity"}:
                    candidates = {column_name.lower(), column_name.replace("_", " ").lower()}
                    if any(_contains_phrase(normalized, candidate) for candidate in candidates):
                        if column_name not in target_columns:
                            target_columns.append(column_name)
                        if table_name not in target_tables:
                            target_tables.append(table_name)

    def _match_numeric_constraints(self, text: str, constraints: list[dict[str, Any]], target_tables: list[str], target_columns: list[str]) -> None:
        normalized = _normalize(text)
        table = "invoice_items"
        column = "unit_price"

        patterns = [
            (r"(?:price|unit price) (?:above|over|more than|greater than) (\-?\d+(?:\.\d+)?)", ">"),
            (r"(?:price|unit price) (?:below|under|less than) (\-?\d+(?:\.\d+)?)", "<"),
            (r"(?:price|unit price) at least (\-?\d+(?:\.\d+)?)", ">="),
            (r"(?:price|unit price) at most (\-?\d+(?:\.\d+)?)", "<="),
            (r"(?:price|unit price) (?:is|equals?|equal to) (\-?\d+(?:\.\d+)?)", "="),
            (r"(?:cost|costing|priced) (?:above|over|more than|greater than) (\-?\d+(?:\.\d+)?)", ">"),
            (r"(?:cost|costing|priced) (?:below|under|less than) (\-?\d+(?:\.\d+)?)", "<"),
            (r"(?:cost|costing|priced) at least (\-?\d+(?:\.\d+)?)", ">="),
            (r"(?:cost|costing|priced) at most (\-?\d+(?:\.\d+)?)", "<="),
            (r"(?:cost|costing|priced) (?:is|equals?|equal to) (\-?\d+(?:\.\d+)?)", "="),
        ]
        for pattern, operator in patterns:
            match = re.search(pattern, normalized)
            if match:
                self._add_constraint(constraints, table, column, operator, _to_number(match.group(1)))
                if table not in target_tables:
                    target_tables.append(table)
                if column not in target_columns:
                    target_columns.append(column)

        between_match = re.search(r"(?:price|unit price|cost|costing|priced) between (\-?\d+(?:\.\d+)?) and (\-?\d+(?:\.\d+)?)", normalized)
        if between_match:
            self._add_constraint(constraints, table, column, "between", [_to_number(between_match.group(1)), _to_number(between_match.group(2))])
            if table not in target_tables:
                target_tables.append(table)
            if column not in target_columns:
                target_columns.append(column)

        contains_match = re.search(r"(?:description|name) (?:contains|like) (.+)$", normalized)
        if contains_match:
            value = contains_match.group(1).strip().strip("'\"")
            self._add_constraint(constraints, "products", "description", "ILIKE", f"%{value}%")
            if "products" not in target_tables:
                target_tables.append("products")
            if "description" not in target_columns:
                target_columns.append("description")

        contains_desc_match = re.search(r"\bproducts?\b.*?contain\s+['\"]?(.+?)['\"]?\s+in\s+the\s+description", normalized)
        if contains_desc_match:
            value = contains_desc_match.group(1).strip().strip("'\"")
            self._add_constraint(constraints, "products", "description", "ILIKE", f"%{value}%")
            if "products" not in target_tables:
                target_tables.append("products")
            if "description" not in target_columns:
                target_columns.append("description")

        invoice_between_dates = re.search(r"invoices?\s+between\s+(\d{4}-\d{2}-\d{2})\s+and\s+(\d{4}-\d{2}-\d{2})", normalized)
        if invoice_between_dates:
            self._add_constraint(
                constraints,
                "invoices",
                "invoice_date",
                "between",
                [invoice_between_dates.group(1), invoice_between_dates.group(2)],
            )
            if "invoices" not in target_tables:
                target_tables.append("invoices")

        date_patterns = [
            (r"before (\d{4}-\d{2}-\d{2})", "<"),
            (r"after (\d{4}-\d{2}-\d{2})", ">"),
            (r"in (\d{4})", "year"),
            (r"from (\d{4})", "year"),
        ]
        for pattern, operator in date_patterns:
            match = re.search(pattern, normalized)
            if match:
                value = match.group(1)
                if operator == "year":
                    value = int(value)
                self._add_constraint(constraints, "invoices", "invoice_date", operator, value)
                if "invoices" not in target_tables:
                    target_tables.append("invoices")

        if "last month" in normalized:
            self._add_constraint(constraints, "invoices", "invoice_date", "last_month", True)
            if "invoices" not in target_tables:
                target_tables.append("invoices")

    def _match_follow_up(self, text: str, previous: ParsedIntent | None) -> tuple[bool, list[str], list[str], list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]], int | None]:
        normalized = _normalize(text)
        follow_up = previous is not None and (
            normalized.startswith("now only")
            or normalized.startswith("same but")
            or normalized.startswith("those")
            or normalized.startswith("and sort by")
            or normalized.startswith("same for")
            or normalized.startswith("the same")
            or normalized.startswith("only for")
        )
        if not follow_up:
            return False, [], [], [], None, [], None

        target_tables = list(previous.target_tables)
        target_columns = list(previous.target_columns)
        constraints = [dict(item) for item in previous.constraints]
        aggregation = dict(previous.aggregation) if previous.aggregation else None
        order_by = [dict(item) for item in previous.order_by]
        limit = previous.limit
        return True, target_tables, target_columns, constraints, aggregation, order_by, limit

    def _match_ddl_entities(self, text: str) -> tuple[list[str], list[str]]:
        normalized = _normalize(text)
        tables: list[str] = []
        columns: list[str] = []

        for table_name in self.table_names:
            if _contains_phrase(normalized, table_name) or _contains_phrase(normalized, table_name.replace("_", " ")):
                tables.append(table_name)

        drop_match = re.search(r"\bdrop\s+(?:the\s+)?(?:table\s+)?([a-z_][a-z0-9_\s]*)", normalized)
        if drop_match:
            phrase = drop_match.group(1).strip()
            for table_name in self.table_names:
                if _contains_phrase(phrase, table_name) or _contains_phrase(phrase, table_name.replace("_", " ")):
                    if table_name not in tables:
                        tables.append(table_name)

        truncate_match = re.search(r"\btruncate\s+([a-z_][a-z0-9_\s]*)", normalized)
        if truncate_match:
            phrase = truncate_match.group(1).strip()
            for table_name in self.table_names:
                if _contains_phrase(phrase, table_name) or _contains_phrase(phrase, table_name.replace("_", " ")):
                    if table_name not in tables:
                        tables.append(table_name)

        alter_table_match = re.search(r"\balter\s+table\s+([a-z_][a-z0-9_]*)", normalized)
        if alter_table_match:
            table_name = alter_table_match.group(1)
            if table_name not in tables:
                tables.append(table_name)

        add_column_match = re.search(r"\badd\s+column\s+([a-z_][a-z0-9_]*)", normalized)
        if add_column_match:
            columns.append(add_column_match.group(1))

        if "create table" in normalized:
            create_match = re.search(r"\bcreate\s+table\s+([a-z_][a-z0-9_]*)", normalized)
            if create_match:
                tables.append(create_match.group(1))

        return list(dict.fromkeys(tables)), list(dict.fromkeys(columns))

    def _classify_action(self, text: str) -> tuple[str, str, bool, list[str]]:
        normalized = _normalize(text)
        ambiguities: list[str] = []

        if re.search(r"\b(drop|truncate|alter|grant|revoke)\b", normalized) or "create table" in normalized:
            if "drop" in normalized:
                return "ddl", "drop_table", True, ambiguities
            if "truncate" in normalized:
                return "ddl", "truncate_table", True, ambiguities
            if "alter" in normalized:
                return "ddl", "alter_schema", False, ambiguities
            if "grant" in normalized or "revoke" in normalized:
                return "ddl", "alter_privileges", False, ambiguities
            if "create table" in normalized:
                return "ddl", "create_table", False, ambiguities

        read_cues = any(_contains_phrase(normalized, cue) for cue in READ_KEYWORDS)
        write_cues = any(_contains_phrase(normalized, cue) for cue in WRITE_KEYWORDS)
        question_cue = normalized.startswith(tuple(READ_ACTION_STARTERS)) or normalized.startswith("how many")
        past_tense_cue = any(token in _words(normalized) for token in PAST_TENSE_TOKENS)

        if question_cue and past_tense_cue:
            ambiguities.append("question asks about past data changes, treat as read")
            if "how many" in normalized or normalized.startswith("count"):
                return "read", "aggregate_count", False, ambiguities
            return "read", "retrieve_records", False, ambiguities

        if "cancel invoice" in normalized:
            return "write", "cancel_invoice", True, ambiguities

        destructive_intent = None
        for phrase, mode in DESTRUCTIVE_SYNONYMS.items():
            if _contains_phrase(normalized, phrase):
                destructive_intent = mode
                break

        if destructive_intent:
            if destructive_intent in {"drop", "truncate"}:
                intent = "drop_table" if destructive_intent == "drop" else "truncate_table"
                return "ddl", intent, True, ambiguities
            return "write", "delete_record", True, ambiguities

        if re.search(r"\bset\s+the\s+[a-z_]+\s+of\s+.+\s+to\s+.+", normalized):
            return "write", "update_record", False, ambiguities

        if read_cues and write_cues:
            ambiguities.append("both read and write cues present; preferring read for question form")
            if question_cue:
                if "how many" in normalized or "count" in normalized:
                    return "read", "aggregate_count", False, ambiguities
                return "read", "retrieve_records", False, ambiguities

        if write_cues:
            if re.search(r"\bdelete\s+everything\b", normalized):
                ambiguities.append("delete request is unscoped")
                return "write", "delete_record", True, ambiguities
            if "delete" in normalized or "remove" in normalized:
                return "write", "delete_record", False, ambiguities
            if "update" in normalized or re.search(r"\bset\s+[a-z_]+\s*(?:to|=)", normalized):
                return "write", "update_record", False, ambiguities
            return "write", "insert_record", False, ambiguities

        if read_cues:
            if "how many" in normalized or normalized.startswith("count"):
                return "read", "aggregate_count", False, ambiguities
            if "total revenue" in normalized or "sum" in normalized:
                return "read", "aggregate_sum", False, ambiguities
            return "read", "retrieve_records", False, ambiguities

        if self._table_matches(text) or self._column_matches(text) or any(country in normalized for country in self.country_values):
            ambiguities.append("resolved as read from schema nouns and known values")
            return "read", "retrieve_records", False, ambiguities

        return "unknown", "unknown", False, ambiguities

    def _detect_aggregation(self, text: str, target_tables: list[str]) -> dict[str, Any] | None:
        normalized = _normalize(text)
        if "total revenue per country" in normalized or ("revenue" in normalized and "country" in normalized):
            if "customers" not in target_tables:
                target_tables.append("customers")
            if "invoice_items" not in target_tables:
                target_tables.append("invoice_items")
            if "invoices" not in target_tables:
                target_tables.append("invoices")
            return {
                "function": "sum",
                "measure": "quantity * unit_price",
                "group_by": ["customers.country"],
            }

        if "how many" in normalized or normalized.startswith("count") or normalized.startswith("number of"):
            return {"function": "count", "measure": "*"}

        avg_match = re.search(r"average (?:price|unit price|quantity)", normalized)
        if avg_match:
            measure = "unit_price" if "price" in avg_match.group(0) else "quantity"
            return {"function": "avg", "measure": measure}

        top_match = re.search(r"top\s+(\d+)\s+(.+?)\s+by\s+(.+)$", normalized)
        if top_match:
            limit = int(top_match.group(1))
            sort_term = top_match.group(3).strip()
            return {"function": "top_n", "limit": limit, "order_by": sort_term}

        return None

    def _build_entities(self, text: str, target_tables: list[str], target_columns: list[str]) -> dict[str, Any]:
        identifiers = sorted({token for token in IDENTIFIER_RE.findall(text) if token.lower() not in READ_KEYWORDS and token.lower() not in WRITE_KEYWORDS})
        return {
            "tables": target_tables,
            "columns": target_columns,
            "quoted_values": _extract_quoted(text),
            "numbers": NUMBER_RE.findall(text),
            "dates": DATE_RE.findall(text),
            "identifiers": identifiers,
        }

    def _parse_ordering(self, text: str) -> tuple[list[dict[str, Any]], int | None]:
        normalized = _normalize(text)
        order_by: list[dict[str, Any]] = []
        limit: int | None = None

        top_match = re.search(r"top\s+(\d+)\b", normalized)
        if top_match:
            limit = int(top_match.group(1))

        if "sort by date" in normalized or "order by date" in normalized:
            order_by.append({"column": "invoice_date", "direction": "asc"})

        if "sort by country" in normalized:
            order_by.append({"column": "country", "direction": "asc"})

        if "sort by revenue" in normalized or "order by revenue" in normalized:
            order_by.append({"column": "revenue", "direction": "desc"})

        if "descending" in normalized or "desc" in normalized:
            if order_by:
                order_by[-1]["direction"] = "desc"

        return order_by, limit

    def _match_examples(self, text: str) -> list[dict[str, Any]]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for example in self.examples:
            score = _token_score(text, str(example.get("input", "")))
            if score > 0:
                scored.append((score, example))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("input", ""))))
        return [dict(example, similarity=round(score, 4)) for score, example in scored[:3]]

    def _confidence(self, action_type: str, intent: str, target_tables: list[str], constraints: list[dict[str, Any]], aggregation: dict[str, Any] | None, ambiguities: list[str], previous: ParsedIntent | None) -> float:
        score = 0.15
        if action_type != "unknown":
            score += 0.3
        if intent != "unknown":
            score += 0.15
        if target_tables:
            score += 0.15
        if constraints:
            score += min(0.15, 0.03 * len(constraints))
        if aggregation:
            score += 0.1
        if previous is not None:
            score += 0.05
        score -= 0.08 * len(ambiguities)
        if action_type == "unknown":
            score -= 0.1
        if not target_tables and intent != "unknown":
            score -= 0.1
        return max(0.0, min(1.0, round(score, 4)))

    def _collect_target_tables(self, text: str, constraints: list[dict[str, Any]]) -> list[str]:
        tables = self._table_matches(text)
        for constraint in constraints:
            table = constraint.get("table")
            column = constraint.get("column")
            if table and table not in tables:
                tables.append(table)
            if not table and column:
                inferred = self._infer_table_for_column(str(column))
                if inferred and inferred not in tables:
                    tables.append(inferred)
        return tables

    def _match_constraints(self, text: str) -> tuple[list[dict[str, Any]], list[str]]:
        target_tables: list[str] = []
        target_columns: list[str] = []
        constraints: list[dict[str, Any]] = []

        self._match_known_values(text, constraints, target_tables, target_columns)
        self._match_basic_equality(text, constraints, target_tables, target_columns)
        self._match_numeric_constraints(text, constraints, target_tables, target_columns)

        # invoice and date filters not covered above.
        normalized = _normalize(text)
        date_year = re.search(r"\b(?:in|from)\s+(20\d{2})\b", normalized)
        if date_year:
            self._add_constraint(constraints, "invoices", "invoice_date", "year", int(date_year.group(1)))
            target_tables.append("invoices")

        if "last month" in normalized:
            self._add_constraint(constraints, "invoices", "invoice_date", "last_month", True)
            target_tables.append("invoices")

        before_after = re.search(r"\b(before|after)\s+(\d{4}-\d{2}-\d{2})\b", normalized)
        if before_after:
            operator = "<" if before_after.group(1) == "before" else ">"
            self._add_constraint(constraints, "invoices", "invoice_date", operator, before_after.group(2))
            target_tables.append("invoices")

        target_tables = list(dict.fromkeys(target_tables))
        target_columns = list(dict.fromkeys(target_columns))

        return constraints, target_tables

    def _extract_set_values(self, text: str, action_type: str, intent: str) -> dict[str, Any]:
        if action_type != "write" or intent != "update_record":
            return {}

        normalized = _normalize(text)
        set_values: dict[str, Any] = {}

        country_update = re.search(r"set\s+the\s+country\s+of\s+customer\s+[0-9]+(?:\.0)?\s+to\s+([a-z\s]+)$", normalized)
        if country_update:
            country_value = country_update.group(1).strip().title()
            set_values["country"] = country_value

        desc_update = re.search(r"change\s+the\s+description\s+of\s+stock\s+code\s+[A-Z0-9]+\s+to\s+['\"]?(.+?)['\"]?$", text, flags=re.IGNORECASE)
        if desc_update:
            set_values["description"] = desc_update.group(1).strip()

        generic_set = re.search(r"\bset\s+([a-z_]+)\s*(?:to|=)\s*['\"]?(.+?)['\"]?$", text, flags=re.IGNORECASE)
        if generic_set and generic_set.group(1).lower() not in {"of"}:
            column = generic_set.group(1).lower()
            value = generic_set.group(2).strip()
            set_values[column] = value

        return set_values

    def _merge_constraints(self, previous_constraints: list[dict[str, Any]], new_constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged = [dict(item) for item in previous_constraints]
        for constraint in new_constraints:
            self._replace_or_add_constraint(merged, dict(constraint))
        return merged

    def parse(self, text: str, session_id: str | None = None, session_context: dict[str, Any] | None = None) -> ParsedIntent:
        session_context = dict(session_context or {})
        previous = self.session_store.get(session_id) if session_id else None
        resolved_from_context = False

        action_type, intent, is_destructive, ambiguities = self._classify_action(text)
        constraints, constraint_tables = self._match_constraints(text)
        target_tables = self._collect_target_tables(text, constraints)
        target_columns = self._column_matches(text)
        set_values = self._extract_set_values(text, action_type, intent)

        if set_values:
            for column, value in set_values.items():
                for constraint in list(constraints):
                    if constraint.get("column") == column and constraint.get("value") == value:
                        constraints.remove(constraint)

                inferred_table = self._infer_table_for_column(column)
                if inferred_table and inferred_table not in target_tables:
                    target_tables.append(inferred_table)
                if column not in target_columns:
                    target_columns.append(column)

        ddl_tables, ddl_columns = self._match_ddl_entities(text)
        if ddl_tables:
            for table in ddl_tables:
                if table not in target_tables:
                    target_tables.append(table)
        if ddl_columns:
            for column in ddl_columns:
                if column not in target_columns:
                    target_columns.append(column)

        if constraint_tables:
            for table in constraint_tables:
                if table not in target_tables:
                    target_tables.append(table)

        follow_up, prev_tables, prev_columns, prev_constraints, prev_aggregation, prev_order_by, prev_limit = self._match_follow_up(text, previous)
        aggregation = self._detect_aggregation(text, target_tables)
        order_by, limit = self._parse_ordering(text)

        if follow_up and previous is not None:
            resolved_from_context = True
            if not target_tables:
                target_tables = list(prev_tables)
            else:
                for table in prev_tables:
                    if table not in target_tables:
                        target_tables.append(table)
            if not target_columns:
                target_columns = list(prev_columns)
            constraints = self._merge_constraints(prev_constraints, constraints)
            if aggregation is None:
                aggregation = prev_aggregation
            if not order_by:
                order_by = prev_order_by
            if limit is None:
                limit = prev_limit
            if previous.raw_text:
                session_context.setdefault("previous_request", previous.raw_text)
            if action_type == "unknown":
                action_type = previous.action_type
            if intent == "unknown":
                intent = previous.intent

        if intent == "unknown" and action_type == "unknown":
            ambiguities.append("no reliable action cues found")

        if action_type == "write" and intent == "delete_record" and not constraints:
            is_destructive = True
            is_unscoped = True
        else:
            is_unscoped = action_type == "write" and not constraints and intent in {"delete_record", "update_record", "cancel_invoice"}

        if action_type == "ddl" and intent in {"drop_table", "truncate_table"}:
            is_destructive = True

        if intent == "cancel_invoice":
            if not any(constraint.get("column") == "invoice_no" for constraint in constraints):
                invoice_match = re.search(r"\bcancel\s+invoice\s+([A-Z]?[0-9]{3,})\b", text, flags=re.IGNORECASE)
                if invoice_match:
                    invoice_no = invoice_match.group(1).upper()
                    self._add_constraint(constraints, "invoices", "invoice_no", "=", invoice_no)
                    if "invoices" not in target_tables:
                        target_tables.append("invoices")
            if not target_tables:
                target_tables.append("invoices")

        if intent in {"aggregate_count", "aggregate_sum"} and not aggregation:
            if intent == "aggregate_count":
                aggregation = {"function": "count", "measure": "*"}
            else:
                aggregation = {"function": "sum", "measure": "quantity * unit_price", "group_by": ["customers.country"]}

        if aggregation is not None:
            if aggregation.get("function") == "sum":
                intent = "aggregate_sum"
            elif aggregation.get("function") == "count":
                intent = "aggregate_count"
            elif aggregation.get("function") == "avg":
                intent = "aggregate_avg"

        if action_type == "unknown" and aggregation is not None:
            action_type = "read"
            if aggregation.get("function") == "sum":
                intent = "aggregate_sum"
            elif aggregation.get("function") == "count":
                intent = "aggregate_count"
            elif aggregation.get("function") == "avg":
                intent = "aggregate_avg"
            elif aggregation.get("function") == "top_n":
                intent = "retrieve_records"

        if intent == "aggregate_sum" and "customers" not in target_tables:
            target_tables.append("customers")
        if intent == "aggregate_sum" and "invoice_items" not in target_tables:
            target_tables.append("invoice_items")

        if action_type == "ddl":
            is_destructive = True

        if action_type == "unknown":
            intent = "unknown"

        if not target_tables and previous is not None and follow_up:
            target_tables = list(previous.target_tables)

        if not target_tables and intent != "unknown":
            ambiguities.append("no table matched directly")

        if target_columns:
            for column in list(target_columns):
                inferred_table = self._infer_table_for_column(column)
                if inferred_table and inferred_table not in target_tables:
                    target_tables.append(inferred_table)

        if aggregation and aggregation.get("function") == "top_n" and limit is None:
            limit = int(aggregation.get("limit", 0)) or None

        if aggregation is None:
            top_match = re.search(r"top\s+(\d+)\b", _normalize(text))
            if top_match:
                limit = int(top_match.group(1))

        confidence = self._confidence(action_type, intent, target_tables, constraints, aggregation, ambiguities, previous)

        entities = self._build_entities(text, target_tables, target_columns)
        if previous is not None:
            session_context.setdefault("previous_action_type", previous.action_type)

        parsed = ParsedIntent(
            raw_text=text,
            action_type=action_type,
            intent=intent,
            target_tables=target_tables,
            target_columns=target_columns,
            constraints=constraints,
            set_values=set_values,
            aggregation=aggregation,
            order_by=order_by,
            limit=limit,
            entities=entities,
            session_context=session_context,
            confidence=confidence,
            ambiguities=ambiguities,
            is_destructive=is_destructive,
            is_unscoped=is_unscoped,
            resolved_from_context=resolved_from_context,
            previous_request=previous.raw_text if previous else None,
            matched_examples=self._match_examples(text),
        )

        if session_id:
            self.session_store.put(session_id, parsed)

        return parsed


def _print_parsed(parsed: ParsedIntent) -> None:
    print(json.dumps(asdict(parsed), indent=2))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Week 3 natural-language intent parser")
    parser.add_argument("text", nargs="?", help="Natural-language request")
    parser.add_argument("--session-context", default="{}", help="JSON session context to preserve")
    parser.add_argument("--session-id", default="", help="Session identifier for memory across calls in one process")
    parser.add_argument("--interactive", action="store_true", help="Start a tiny REPL for demoing follow-up handling")
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path(__file__).with_name("schema_metadata.json"),
        help="Path to schema metadata JSON",
    )
    parser.add_argument(
        "--examples",
        type=Path,
        default=Path(__file__).with_name("intent_examples.json"),
        help="Path to few-shot examples JSON",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    intent_parser = IntentParser.from_paths(metadata_path=args.metadata, examples_path=args.examples)
    session_context = json.loads(args.session_context)
    if args.session_id:
        session_context.setdefault("session_id", args.session_id)

    if args.interactive:
        session_id = args.session_id or "interactive"
        while True:
            try:
                text = input("intent> ").strip()
            except EOFError:
                break
            if not text or text.lower() in {"exit", "quit"}:
                break
            parsed = intent_parser.parse(text, session_id=session_id, session_context=session_context)
            _print_parsed(parsed)
        return 0

    if not args.text:
        parser.error("text is required unless --interactive is used")

    parsed = intent_parser.parse(args.text, session_id=args.session_id or None, session_context=session_context)
    _print_parsed(parsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())