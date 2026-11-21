"""Offline planner for the web app: works without an LLM API key.

The ReAct agent normally asks an LLM (Groq/OpenAI) for the SQL plan. Without a
key, this client stands in for the LLM:

  1. requests from the scripted demo scenarios get their scripted plans (so
     the Week 6-9 demos, including their deliberate first-attempt mistakes,
     behave exactly as documented);
  2. everything else is translated deterministically from the Week 3 parsed
     intent (target tables, constraints, SET values, aggregation, ordering)
     plus a few extra patterns (text search in descriptions, month/year date
     ranges), using the foreign-key join paths from schema_metadata.json.

It returns the same tool-call JSON an LLM would, so the plan still goes
through the validator, guardrails, Week 9 checks and the sandboxed executor.
Requests it cannot express are returned as a blocked plan saying so.

`sketch_sql()` builds the SQL a request *would* have needed even when the
pipeline refuses it before planning (DDL, unscoped writes); the web app shows
it, clearly marked as never sent to the database.
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

try:
    from .demo_llm import ScriptedLLMClient
except ImportError:
    from demo_llm import ScriptedLLMClient

METADATA = json.loads(Path(__file__).with_name("schema_metadata.json").read_text(encoding="utf-8"))
TABLE_COLUMNS: dict[str, list[str]] = {
    t["name"]: [c["name"] for c in t.get("columns", [])] for t in METADATA["tables"]
}
PRIMARY_KEYS: dict[str, list[str]] = {t["name"]: list(t.get("primary_key", [])) for t in METADATA["tables"]}
NUMERIC_COLUMNS = {"quantity", "unit_price", "invoice_item_id"}
NOUNS = [
    (r"line[\s_-]?items?|order lines?|invoice[\s_]items?", "invoice_items"),
    (r"invoices?|orders?", "invoices"),
    (r"customers?|clients?", "customers"),
    (r"products?", "products"),
]
DEFAULT_COLUMNS = {
    "customers": ["customer_id", "country"],
    "products": ["stock_code", "description"],
    "invoices": ["invoice_no", "invoice_date", "customer_id", "status"],
    "invoice_items": ["invoice_no", "stock_code", "quantity", "unit_price"],
}
ORDER_ALIASES = {"price": "unit_price", "unit price": "unit_price", "quantity": "quantity", "qty": "quantity",
                 "date": "invoice_date", "country": "country", "description": "description"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
OPERATORS = {"=", "<>", "!=", ">", "<", ">=", "<="}


class PlanningError(ValueError):
    """The rule planner cannot express this request."""


def _edges() -> dict[str, list[tuple[str, str, str]]]:
    """table -> [(neighbour, join condition)] from the metadata foreign keys (both directions)."""
    graph: dict[str, list[tuple[str, str, str]]] = {name: [] for name in TABLE_COLUMNS}
    for table in METADATA["tables"]:
        for fk in table.get("foreign_keys", []):
            match = re.fullmatch(r"\s*(\w+)\s*\(\s*(\w+)\s*\)\s*", fk["references"])
            if not match:
                continue
            parent, parent_col = match.groups()
            child_col = fk["columns"][0]
            condition = f"{parent}.{parent_col} = {table['name']}.{child_col}"
            graph[table["name"]].append((parent, condition, child_col))
            graph[parent].append((table["name"], condition, parent_col))
    return graph


GRAPH = _edges()


def join_path(start: str, goal: str) -> list[tuple[str, str]]:
    """[(table, ON condition)] to reach `goal` from `start` along foreign keys."""
    if start == goal:
        return []
    queue: deque[tuple[str, list[tuple[str, str]]]] = deque([(start, [])])
    seen = {start}
    while queue:
        table, path = queue.popleft()
        for neighbour, condition, _ in GRAPH[table]:
            if neighbour in seen:
                continue
            step = path + [(neighbour, condition)]
            if neighbour == goal:
                return step
            seen.add(neighbour)
            queue.append((neighbour, step))
    raise PlanningError(f"no foreign-key path from {start} to {goal}")


def literal(column: str, value: Any) -> str:
    if column in NUMERIC_COLUMNS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if column in NUMERIC_COLUMNS:
        try:
            float(str(value))
            return str(value)
        except ValueError:
            pass
    return "'" + str(value).replace("'", "''") + "'"


def nouns_in(text: str) -> list[str]:
    found = []
    for pattern, table in NOUNS:
        for match in re.finditer(rf"\b(?:{pattern})\b", text.lower()):
            found.append((match.start(), table))
    return [table for _, table in sorted(found)]


class QueryBuilder:
    def __init__(self, request: str, parsed: dict[str, Any]) -> None:
        self.request = request
        self.parsed = parsed
        self.text = request.lower()

    # ------------------------------------------------------------ helpers
    def main_table(self) -> str:
        targets = self.parsed.get("target_tables") or []
        set_columns = list((self.parsed.get("set_values") or {}).keys())
        if self.parsed.get("intent") == "update_record" and set_columns:
            owners = [t for t, cols in TABLE_COLUMNS.items() if all(c in cols for c in set_columns)]
            constraint_cols = {c["column"] for c in self.parsed.get("constraints") or []}
            owners.sort(key=lambda t: (-len(constraint_cols & set(TABLE_COLUMNS[t])), t not in targets))
            if owners:
                return owners[0]
        aggregation = self.parsed.get("aggregation") or {}
        measure = aggregation.get("measure")
        if measure and measure != "*" and aggregation.get("function") != "top_n":
            owners = [t for t, cols in TABLE_COLUMNS.items() if measure.split(".")[-1] in cols]
            if owners:
                return "invoice_items" if "invoice_items" in owners else owners[0]
        for source in (self.request, self.parsed.get("previous_request") or ""):
            found = nouns_in(source)
            if found:
                return found[0]
        if targets:
            return targets[-1]
        raise PlanningError("could not tell which table the request is about")

    def column_ref(self, main: str, table: str | None, column: str, joins: dict[str, str]) -> str:
        if column in TABLE_COLUMNS[main]:
            return f"{main}.{column}"
        owner = table if table and column in TABLE_COLUMNS.get(table, []) else next(
            (t for t, cols in TABLE_COLUMNS.items() if column in cols), None)
        if owner is None:
            raise PlanningError(f"unknown column {column}")
        for joined, condition in join_path(main, owner):
            joins.setdefault(joined, condition)
        return f"{owner}.{column}"

    def conditions(self, main: str, joins: dict[str, str], *, allow_joins: bool = True) -> list[str]:
        where = []
        for constraint in self.parsed.get("constraints") or []:
            column, operator, value = constraint["column"], constraint["operator"], constraint["value"]
            if not allow_joins and column not in TABLE_COLUMNS[main]:
                raise PlanningError(f"a write on {main} cannot filter on {constraint['table']}.{column} offline")
            ref = self.column_ref(main, constraint.get("table"), column, joins)
            if operator in OPERATORS:
                where.append(f"{ref} {'<>' if operator == '!=' else operator} {literal(column, value)}")
            elif operator.lower() in {"like", "contains", "ilike"}:
                where.append(f"{ref} ILIKE {literal(column, '%' + str(value).strip('%') + '%')}")
            elif operator.lower() == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
                where.append(f"{ref} BETWEEN {literal(column, value[0])} AND {literal(column, value[1])}")
            else:
                raise PlanningError(f"operator {operator!r} is not supported offline")
        # text search in product descriptions: "with heart in the description", "containing lantern"
        match = (re.search(r"with (?:the word )?['\"]?([\w-]+(?: [\w-]+)?)['\"]? in (?:the|their|its) "
                           r"(?:description|name)", self.text)
                 or re.search(r"(?:description|name)s? (?:contains?|containing|like|with) ['\"]?([\w-]+)", self.text)
                 or re.search(r"\bcontaining ['\"]?([\w-]+)", self.text))
        if match and allow_joins:
            ref = self.column_ref(main, "products", "description", joins)
            where.append(f"{ref} ILIKE {literal('description', '%' + match.group(1).upper() + '%')}")
        # month / year ranges on invoice dates
        month = re.search(rf"\b({'|'.join(MONTHS)})\s+(\d{{4}})\b", self.text)
        year = re.search(r"\b(?:in|from|during)\s+(\d{4})\b", self.text)
        if (month or year) and allow_joins and "invoice_date" not in {c["column"] for c in
                                                                         self.parsed.get("constraints") or []}:
            ref = self.column_ref(main, "invoices", "invoice_date", joins)
            if month:
                m, y = MONTHS.index(month.group(1)) + 1, int(month.group(2))
                end = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
                where.append(f"{ref} >= '{y}-{m:02d}-01' AND {ref} < '{end}'")
            else:
                y = int(year.group(1))
                where.append(f"{ref} >= '{y}-01-01' AND {ref} < '{y + 1}-01-01'")
        return where

    @staticmethod
    def _from(main: str, joins: dict[str, str]) -> str:
        return f"FROM {main}" + "".join(f" JOIN {table} ON {condition}" for table, condition in joins.items())

    # --------------------------------------------------------------- read
    def read(self) -> tuple[str, list[str], str]:
        main = self.main_table()
        joins: dict[str, str] = {}
        where = self.conditions(main, joins)
        limit = min(int(self.parsed.get("limit") or 100), 100)
        aggregation = self.parsed.get("aggregation") or {}
        function = (aggregation.get("function") or "").lower()
        if function in {"count", "sum", "avg", "min", "max"}:
            measure = aggregation.get("measure") or "*"
            measure_ref = "*" if measure == "*" else self.column_ref(main, None, measure.split(".")[-1], joins)
            groups = [self.column_ref(main, g.split(".")[0] if "." in g else None, g.split(".")[-1], joins)
                      for g in aggregation.get("group_by") or []]
            alias = f"{function}_{measure.split('.')[-1]}" if measure != "*" else "count"
            select = ", ".join(groups + [f"{function.upper()}({measure_ref}) AS {alias}"])
            sql = f"SELECT {select} {self._from(main, joins)}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            if groups:
                sql += " GROUP BY " + ", ".join(groups) + f" ORDER BY {alias} DESC"
            sql += f" LIMIT {limit}"
            explanation = f"{function.upper()} of {measure} from {main}" + (
                f" grouped by {', '.join(groups)}" if groups else "")
            return sql, [main, *joins], explanation
        columns = [f"{main}.{c}" for c in DEFAULT_COLUMNS[main]]
        order = ""
        order_word = aggregation.get("order_by") if function == "top_n" else None
        sorted_by = re.search(r"(?:sorted|ordered|order) by (unit price|price|quantity|qty|date|country|description)",
                              self.text)
        if order_word or sorted_by:
            column = ORDER_ALIASES.get((order_word or sorted_by.group(1)).lower(), "unit_price")
            ref = self.column_ref(main, None, column, joins)
            if ref not in columns:
                columns.append(ref)
            descending = function == "top_n" or "desc" in self.text or "highest" in self.text
            order = f" ORDER BY {ref}{' DESC' if descending else ''}"
        sql = f"SELECT {', '.join(columns)} {self._from(main, joins)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f"{order} LIMIT {limit}"
        return sql, [main, *joins], f"Rows from {main}" + (" matching " + " AND ".join(where) if where else "")

    # -------------------------------------------------------------- write
    def write(self, *, require_where: bool = True) -> tuple[str, str | None, list[str], str]:
        intent = self.parsed.get("intent")
        main = self.main_table()
        if intent == "insert_record":
            return self._insert(main)
        joins: dict[str, str] = {}
        where = self.conditions(main, joins, allow_joins=False)
        if require_where and not where:
            raise PlanningError("the change has no filter")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        count_sql = f"SELECT COUNT(*) FROM {main}{clause}"
        if intent == "update_record":
            values = self.parsed.get("set_values") or {}
            if not values:
                raise PlanningError("no new value found (use 'set <column> of ... to <value>')")
            sets = ", ".join(f"{column} = {literal(column, value)}" for column, value in values.items())
            return (f"UPDATE {main} SET {sets}{clause}", count_sql, [main],
                    f"Set {sets} on {main} rows" + (f" where {' AND '.join(where)}" if where else ""))
        if intent == "delete_record":
            return (f"DELETE FROM {main}{clause}", count_sql, [main],
                    f"Delete {main} rows" + (f" where {' AND '.join(where)}" if where else ""))
        raise PlanningError(f"write intent {intent!r} is not supported offline")

    def _insert(self, main: str) -> tuple[str, str, list[str], str]:
        values: dict[str, Any] = {}
        for constraint in self.parsed.get("constraints") or []:
            if constraint["operator"] == "=" and constraint["column"] in TABLE_COLUMNS[main]:
                values[constraint["column"]] = constraint["value"]
        values.update({k: v for k, v in (self.parsed.get("set_values") or {}).items() if k in TABLE_COLUMNS[main]})
        if main == "products":
            match = re.search(r"description\s+['\"]?(.+?)['\"]?\s*$", self.request, re.IGNORECASE)
            if match:
                values["description"] = match.group(1).strip()
        if main == "customers":
            match = re.search(r"\b(?:from|in|country)\s+([A-Z][\w ]+?)\s*$", self.request)
            if match:
                values["country"] = match.group(1)
        keys = PRIMARY_KEYS.get(main, [])
        if main not in {"products", "customers"} or not values or not all(k in values for k in keys):
            raise PlanningError(f"inserting into {main} is not supported offline (needs every key column)")
        columns = list(values)
        sql = (f"INSERT INTO {main} ({', '.join(columns)}) VALUES "
               f"({', '.join(literal(c, values[c]) for c in columns)})")
        count = f"SELECT COUNT(*) FROM {main} WHERE " + " AND ".join(f"{k} = {literal(k, values[k])}" for k in keys)
        return sql, count, [main], f"Insert one row into {main}"


def sketch_sql(request: str, parsed: dict[str, Any]) -> str | None:
    """SQL the request would need, built even for requests the pipeline refuses before planning."""
    action = parsed.get("action_type")
    intent = (parsed.get("intent") or "").lower()
    targets = parsed.get("target_tables") or nouns_in(request)
    if action == "ddl":
        table = (nouns_in(request) or targets or ["<table>"])[0]
        for word, sql in (("truncate", f"TRUNCATE TABLE {table}"), ("drop", f"DROP TABLE {table}"),
                          ("alter", f"ALTER TABLE {table} ..."), ("grant", f"GRANT ... ON {table} TO ..."),
                          ("revoke", f"REVOKE ... ON {table} FROM ..."), ("create", f"CREATE TABLE {table} (...)")):
            if word in intent or word in request.lower():
                return sql
        return f"-- DDL on {table}"
    builder = QueryBuilder(request, parsed)
    try:
        if action == "write":
            return builder.write(require_where=False)[0]
        if action == "read":
            return builder.read()[0]
    except (PlanningError, KeyError, ValueError):
        return None
    return None


class OfflinePlannerClient:
    """LLM stand-in: scripted demo plans first, then deterministic rules."""

    name = "offline rule-based planner (no LLM)"

    def __init__(self) -> None:
        self.scripted = ScriptedLLMClient()
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, user: str, tool_schema: dict, *, context: str | None = None) -> dict:
        payload, _ = json.JSONDecoder().raw_decode(user)
        request = str(payload.get("request", ""))
        parsed = payload.get("parsed_intent", {}) or {}
        action = parsed.get("action_type", "read")
        self.calls.append({"request": request, "user_prompt": user})
        # After a database error, the same rule plan would fail again; after an empty result,
        # returning the same SQL is exactly how the agent confirms "no rows match".
        feedback = "FAILED when executed" in user
        builder = QueryBuilder(request, parsed)
        try:
            if feedback:  # the rule plan already failed once: let the scripted planner try its correction
                raise PlanningError("rule plan failed at execution")
            if action == "write":
                sql, count_sql, tables, explanation = builder.write()
            else:
                sql, tables, explanation = builder.read()
                count_sql = None
        except (PlanningError, KeyError, ValueError) as error:
            scripted = self.scripted.complete(system, user, tool_schema, context=context)
            if not scripted.get("blocked"):
                scripted["explanation"] = "[scripted demo plan] " + scripted.get("explanation", "")
                return scripted
            if feedback:
                error = PlanningError("no corrected plan is available offline")
            return {
                "action_type": action if action in {"read", "write"} else "read", "sql": "", "tables": [],
                "columns": [], "explanation": "Offline planner.", "confidence": 0.0, "count_sql": None,
                "blocked": True,
                "block_reason": (f"The offline rule-based planner cannot express this request ({error}). "
                                 "Rephrase it more simply, or set GROQ_API_KEY to use the LLM planner."),
            }
        return {
            "action_type": action, "sql": sql, "tables": tables, "columns": [], "count_sql": count_sql,
            "explanation": f"[offline rule-based planner] {explanation}.", "confidence": 0.6,
            "blocked": False, "block_reason": None,
        }
