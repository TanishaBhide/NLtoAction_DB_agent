"""Week 9 novelty module: plan-before-apply blast-radius preview.

Before a write is applied, the agent asks: "if this runs, what ELSE changes?"
Guardrails (Week 5) only count the rows the statement itself matches, and the
executor (Week 6) only sees them after the fact. The preview reads PostgreSQL's
own catalog (pg_constraint, pg_trigger) and follows the foreign-key graph from
the target rows, inside a READ ONLY transaction, so nothing is changed:

    direct      rows matched by the write's WHERE (or the VALUES count)
    cascades    ON DELETE / ON UPDATE CASCADE rows in child tables (recursive)
    set null    child rows whose foreign key would be nulled (or defaulted)
    blockers    RESTRICT / NO ACTION references  -> predicted 23503 failure
    parents     INSERT/UPDATE values pointing at a missing parent -> predicted 23503
    duplicates  INSERT of an existing primary key -> predicted 23505
    triggers    user triggers that would fire on every touched table
    spread      how many distinct parent entities the direct rows belong to

The preview is then classified CONTAINED / MODERATE / WIDE. A WIDE radius
escalates ALLOW to CONFIRM (it can never relax a verdict), and after the
sandboxed execution the prediction is checked against what really happened
and stored in blast_radius_previews.

    python database/blast_radius.py "DELETE FROM invoices WHERE invoice_no = '536365'"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

try:
    from .db import DEMO_DATABASE_URL, connect, has_database_config
    from .guardrails import CONFIRM_ROW_THRESHOLD
except ImportError:
    from db import DEMO_DATABASE_URL, connect, has_database_config
    from guardrails import CONFIRM_ROW_THRESHOLD

PREVIEW_TIMEOUT_MS = 3000
MAX_DEPTH = 4
SAMPLE_ROWS = 5
WIDE_FRACTION = 0.5
MODERATE_FRACTION = 0.2
MODERATE_ROWS = 10

FK_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}

FK_SQL = """
SELECT con.conname::text,
       child.relname::text AS child_table,
       parent.relname::text AS parent_table,
       ARRAY(SELECT a.attname::text
             FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
             JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             ORDER BY k.ord) AS child_cols,
       ARRAY(SELECT a.attname::text
             FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
             JOIN pg_attribute AS a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
             ORDER BY k.ord) AS parent_cols,
       con.confdeltype::text,
       con.confupdtype::text,
       ARRAY(SELECT a.attname::text
             FROM unnest(con.conkey) AS k(attnum)
             JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             WHERE a.attnotnull) AS not_null_cols
FROM pg_constraint AS con
JOIN pg_class AS child ON child.oid = con.conrelid
JOIN pg_class AS parent ON parent.oid = con.confrelid
JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
WHERE con.contype = 'f' AND ns.nspname = current_schema()
ORDER BY con.conname
"""

PK_SQL = """
SELECT cls.relname::text,
       ARRAY(SELECT a.attname::text
             FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
             JOIN pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
             ORDER BY k.ord)
FROM pg_constraint AS con
JOIN pg_class AS cls ON cls.oid = con.conrelid
JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
WHERE con.contype = 'p' AND ns.nspname = current_schema()
"""

TRIGGER_SQL = """
SELECT cls.relname::text, t.tgname::text, t.tgtype::int,
       ARRAY(SELECT a.attname::text FROM pg_attribute AS a
             WHERE a.attrelid = t.tgrelid AND a.attnum = ANY (t.tgattr))
FROM pg_trigger AS t
JOIN pg_class AS cls ON cls.oid = t.tgrelid
JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
WHERE NOT t.tgisinternal AND ns.nspname = current_schema()
ORDER BY cls.relname, t.tgname
"""

# pg_trigger.tgtype bits
TG_BEFORE, TG_INSERT, TG_DELETE, TG_UPDATE, TG_INSTEAD = 2, 4, 8, 16, 64


@dataclass(frozen=True)
class ForeignKey:
    name: str
    child: str
    parent: str
    child_cols: tuple[str, ...]
    parent_cols: tuple[str, ...]
    on_delete: str
    on_update: str
    not_null_cols: tuple[str, ...]


@dataclass(frozen=True)
class Trigger:
    table: str
    name: str
    tgtype: int
    columns: tuple[str, ...]

    def fires_on(self, event: str, columns: set[str] | None = None) -> bool:
        bit = {"INSERT": TG_INSERT, "DELETE": TG_DELETE, "UPDATE": TG_UPDATE}[event]
        if not self.tgtype & bit:
            return False
        if event == "UPDATE" and self.columns and columns is not None:
            return bool(set(self.columns) & columns)
        return True

    def label(self, event: str) -> str:
        timing = "INSTEAD OF" if self.tgtype & TG_INSTEAD else ("BEFORE" if self.tgtype & TG_BEFORE else "AFTER")
        return f"{self.table}.{self.name} ({timing} {event})"


@dataclass
class Catalog:
    foreign_keys: list[ForeignKey]
    primary_keys: dict[str, tuple[str, ...]]
    triggers: list[Trigger]

    def referencing(self, table: str) -> list[ForeignKey]:
        """Foreign keys in child tables that point AT `table`."""
        return [fk for fk in self.foreign_keys if fk.parent == table]

    def references_from(self, table: str) -> list[ForeignKey]:
        """Foreign keys declared ON `table` (it is the child)."""
        return [fk for fk in self.foreign_keys if fk.child == table]

    def triggers_for(self, table: str, event: str, columns: set[str] | None = None) -> list[str]:
        return [t.label(event) for t in self.triggers if t.table == table and t.fires_on(event, columns)]


@dataclass
class TableImpact:
    table: str
    effect: str  # direct_update | direct_delete | direct_insert | cascade_delete | cascade_update | set_null | set_default | restrict
    rows: int
    via: str | None = None  # constraint name
    depth: int = 0

    def describe(self) -> str:
        words = {
            "direct_update": "updated directly",
            "direct_delete": "deleted directly",
            "direct_insert": "inserted",
            "cascade_delete": "deleted by ON DELETE CASCADE",
            "cascade_update": "updated by ON UPDATE CASCADE",
            "set_null": "foreign key set to NULL",
            "set_default": "foreign key reset to its default",
            "restrict": "still reference the rows (RESTRICT / NO ACTION)",
        }
        via = f" via {self.via}" if self.via else ""
        return f"{self.table}: {self.rows} row(s) {words.get(self.effect, self.effect)}{via}"


@dataclass
class BlastRadius:
    statement_type: str
    target_table: str
    direct_rows: int | None = None
    table_rows: int | None = None
    impacts: list[TableImpact] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    spread: dict[str, int] = field(default_factory=dict)
    sample_columns: list[str] = field(default_factory=list)
    sample_rows: list[list[Any]] = field(default_factory=list)
    predicted_failure: str | None = None
    predicted_sqlstate: str | None = None
    level: str = "UNKNOWN"
    duration_ms: float = 0.0
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    # filled by compare() after the sandboxed execution
    actual_affected_rows: int | None = None
    actual_sqlstate: str | None = None
    prediction_correct: bool | None = None

    # ------------------------------------------------------------ derived
    @property
    def cascaded_rows(self) -> int:
        return sum(i.rows for i in self.impacts if i.effect in {"cascade_delete", "cascade_update", "set_null", "set_default"})

    @property
    def total_rows(self) -> int:
        return (self.direct_rows or 0) + self.cascaded_rows

    @property
    def tables_touched(self) -> int:
        tables = {self.target_table} if self.direct_rows else set()
        tables |= {i.table for i in self.impacts if i.effect not in {"restrict"} and not i.effect.startswith("direct")}
        return len(tables)

    @property
    def max_depth(self) -> int:
        return max((i.depth for i in self.impacts if i.effect != "restrict"), default=0)

    @property
    def fraction(self) -> float | None:
        if self.direct_rows is None or not self.table_rows or self.statement_type == "INSERT":
            return None
        return self.direct_rows / self.table_rows

    def predicted_changes(self) -> dict[str, dict[str, int]]:
        """Per-table row changes the preview predicts if the write succeeds."""
        changes: dict[str, dict[str, int]] = {}
        kind = {"DELETE": "deleted", "UPDATE": "updated", "INSERT": "inserted"}[self.statement_type]
        if self.direct_rows:
            changes.setdefault(self.target_table, {})[kind] = self.direct_rows
        for impact in self.impacts:
            bucket = {"cascade_delete": "deleted", "cascade_update": "updated",
                      "set_null": "updated", "set_default": "updated"}.get(impact.effect)
            if bucket:
                slot = changes.setdefault(impact.table, {})
                slot[bucket] = slot.get(bucket, 0) + impact.rows
        return changes

    def summary(self) -> str:
        if self.error:
            return f"blast radius UNKNOWN ({self.error})"
        verb = {"DELETE": "deletes", "UPDATE": "updates", "INSERT": "inserts"}[self.statement_type]
        text = f"{self.level}: {verb} {self.direct_rows} row(s) in {self.target_table}"
        if self.fraction is not None:
            text += f" ({self.fraction:.0%} of the table)"
        cascades = [i for i in self.impacts if i.effect != "restrict" and not i.effect.startswith("direct")]
        if cascades:
            text += "; then " + "; ".join(i.describe() for i in cascades)
        if self.spread:
            text += "; spans " + ", ".join(f"{n} {entity(table, n)}" for table, n in self.spread.items())
        if self.predicted_failure:
            text += f"; PREDICTED FAILURE {self.predicted_sqlstate}: {self.predicted_failure}"
        return text

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(cascaded_rows=self.cascaded_rows, total_rows=self.total_rows,
                    tables_touched=self.tables_touched, max_depth=self.max_depth, fraction=self.fraction)
        return json.loads(json.dumps(data, default=str))

    # ------------------------------------------------------- verification
    def compare(self, execution: Any) -> None:
        """Check the prediction against the sandboxed execution that followed it."""
        if execution is None or getattr(execution, "status", None) == "not_executed":
            return
        error = getattr(execution, "error", None)
        self.actual_sqlstate = getattr(error, "sqlstate", None) if error else None
        self.actual_affected_rows = getattr(execution, "affected_rows", None)
        if self.error:
            return
        if self.predicted_sqlstate:
            # A predicted constraint failure is correct if the database raised it.
            self.prediction_correct = bool(self.actual_sqlstate) and self.actual_sqlstate == self.predicted_sqlstate
        elif self.actual_sqlstate and self.actual_sqlstate.startswith("23"):
            self.prediction_correct = False  # an integrity failure the preview did not foresee
        elif self.actual_affected_rows is not None:
            self.prediction_correct = self.actual_affected_rows == self.direct_rows
        elif self.actual_sqlstate:
            self.prediction_correct = None  # failed for a reason outside the preview's scope


def entity(table: str, count: int) -> str:
    """'1 invoice', '3 invoices' (parent tables here are plain plurals)."""
    return table[:-1] if count == 1 and table.endswith("s") else table


def q(name: str) -> str:
    return exp.to_identifier(name).sql(dialect="postgres")


def _cols(columns: tuple[str, ...] | list[str], alias: str | None = None) -> str:
    names = [f"{alias}.{q(c)}" if alias else q(c) for c in columns]
    return names[0] if len(names) == 1 else "(" + ", ".join(names) + ")"


def _literal_sql(node: exp.Expression) -> str | None:
    """SQL text of a constant value, or None when the value is not a constant."""
    inner = node
    while isinstance(inner, (exp.Cast, exp.Paren)):
        inner = inner.this
    if isinstance(inner, exp.Neg):
        inner = inner.this
    if isinstance(inner, exp.Literal):
        return node.sql(dialect="postgres")
    return None


def _is_null(node: exp.Expression) -> bool:
    return isinstance(node, exp.Null)


class BlastRadiusAnalyzer:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        max_depth: int = MAX_DEPTH,
        sample_rows: int = SAMPLE_ROWS,
        wide_rows: int = CONFIRM_ROW_THRESHOLD,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> None:
        self.database_url = database_url
        self.max_depth = max_depth
        self.sample_rows = sample_rows
        self.wide_rows = wide_rows
        self.timeout_ms = timeout_ms
        self._catalog: Catalog | None = None

    # ------------------------------------------------------------ catalog
    def load_catalog(self, cursor: Any) -> Catalog:
        if self._catalog is None:
            cursor.execute(FK_SQL)
            fks = [ForeignKey(r[0], r[1], r[2], tuple(r[3]), tuple(r[4]), r[5], r[6], tuple(r[7]))
                   for r in cursor.fetchall()]
            cursor.execute(PK_SQL)
            pks = {r[0]: tuple(r[1]) for r in cursor.fetchall()}
            cursor.execute(TRIGGER_SQL)
            triggers = [Trigger(r[0], r[1], int(r[2]), tuple(r[3])) for r in cursor.fetchall()]
            self._catalog = Catalog(fks, pks, triggers)
        return self._catalog

    @staticmethod
    def _count(cursor: Any, rowset_sql: str) -> int:
        cursor.execute(f"SELECT COUNT(*) FROM ({rowset_sql}) AS blast_rows")
        return int(cursor.fetchone()[0])

    # ------------------------------------------------------------ preview
    def preview(self, plan_or_sql: Any) -> BlastRadius:
        started = time.perf_counter()
        sql = plan_or_sql if isinstance(plan_or_sql, str) else (getattr(plan_or_sql, "sql", "") or "")
        try:
            statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except Exception as error:  # noqa: BLE001
            statements, parse_error = [], str(error)
        else:
            parse_error = "expected exactly one statement"
        if len(statements) != 1 or not isinstance(statements[0], (exp.Insert, exp.Update, exp.Delete)):
            radius = BlastRadius("UPDATE", "?", error=f"not a single INSERT/UPDATE/DELETE ({parse_error})")
            radius.statement_type = "INSERT" if sql.lstrip().upper().startswith("INSERT") else radius.statement_type
            return radius
        statement = statements[0]
        kind = type(statement).__name__.upper()
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        radius = BlastRadius(kind, target.name)

        connection = None
        try:
            connection = connect(self.database_url)
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}")
                catalog = self.load_catalog(cursor)
                if isinstance(statement, exp.Insert):
                    self._preview_insert(cursor, catalog, statement, radius)
                else:
                    self._preview_update_delete(cursor, catalog, statement, radius)
            connection.rollback()
        except Exception as error:  # noqa: BLE001 - a preview failure must never break the agent
            radius.error = f"{type(error).__name__}: {str(error).strip().splitlines()[0] if str(error).strip() else ''}"
            radius.level = "UNKNOWN"
        finally:
            if connection is not None:
                connection.close()
        if not radius.error:
            radius.level = self.classify(radius)
        radius.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return radius

    def classify(self, radius: BlastRadius) -> str:
        fraction = radius.fraction or 0.0
        if (radius.total_rows > self.wide_rows
                or (fraction >= WIDE_FRACTION and (radius.direct_rows or 0) > 1)
                or radius.max_depth >= 2
                or radius.tables_touched >= 3):
            return "WIDE"
        if radius.cascaded_rows > 0 or fraction >= MODERATE_FRACTION or radius.total_rows > MODERATE_ROWS:
            return "MODERATE"
        return "CONTAINED"

    def _predict(self, radius: BlastRadius, sqlstate: str, message: str) -> None:
        if radius.predicted_failure is None:  # keep the first failure PostgreSQL would hit
            radius.predicted_sqlstate, radius.predicted_failure = sqlstate, message

    # ----------------------------------------------------- UPDATE / DELETE
    def _preview_update_delete(self, cursor: Any, catalog: Catalog, statement: exp.Expression,
                               radius: BlastRadius) -> None:
        table = radius.target_table
        where = statement.args.get("where")
        rowset = f"SELECT * FROM {q(table)}" + (f" WHERE {where.this.sql(dialect='postgres')}" if where else "")
        radius.direct_rows = self._count(cursor, rowset)
        cursor.execute(f"SELECT COUNT(*) FROM {q(table)}")
        radius.table_rows = int(cursor.fetchone()[0])
        cursor.execute(f"{rowset} LIMIT {int(self.sample_rows)}")
        radius.sample_columns = [d[0] for d in cursor.description or []]
        radius.sample_rows = [list(r) for r in cursor.fetchall()]
        radius.impacts.append(TableImpact(table, "direct_delete" if isinstance(statement, exp.Delete)
                                          else "direct_update", radius.direct_rows))

        # spread: distinct parent entities the direct rows belong to
        for fk in catalog.references_from(table):
            cursor.execute(f"SELECT COUNT(DISTINCT {_cols(fk.child_cols, 'p')}) FROM ({rowset}) AS p")
            distinct = int(cursor.fetchone()[0])
            if distinct:
                radius.spread[fk.parent] = distinct

        if isinstance(statement, exp.Delete):
            radius.triggers += catalog.triggers_for(table, "DELETE")
            if radius.direct_rows:
                self._walk_delete(cursor, catalog, table, rowset, 1, {table}, radius)
            return

        assignments = {
            a.this.name: a.expression
            for a in statement.expressions
            if isinstance(a, exp.EQ) and isinstance(a.this, exp.Column)
        }
        changed = set(assignments)
        radius.triggers += catalog.triggers_for(table, "UPDATE", changed)
        if not radius.direct_rows:
            return
        # the new values must point at existing parents
        for fk in catalog.references_from(table):
            if set(fk.child_cols) <= changed:
                self._check_parent(cursor, fk, [assignments[c] for c in fk.child_cols], radius)
        # NOT NULL / key changes on the target are left to PostgreSQL; children follow the key
        self._walk_update(cursor, catalog, table, rowset, changed, 1, {table}, radius)

    def _walk_delete(self, cursor: Any, catalog: Catalog, table: str, rowset: str, depth: int,
                     path: set[str], radius: BlastRadius) -> None:
        for fk in catalog.referencing(table):
            if fk.child in path:
                radius.notes.append(f"cycle via {fk.name} not followed")
                continue
            child_rows = (f"SELECT * FROM {q(fk.child)} WHERE {_cols(fk.child_cols)} IN "
                          f"(SELECT {', '.join('p' + str(depth) + '.' + q(c) for c in fk.parent_cols)} "
                          f"FROM ({rowset}) AS p{depth})")
            count = self._count(cursor, child_rows)
            if not count:
                continue
            action = fk.on_delete
            if action == "c":
                radius.impacts.append(TableImpact(fk.child, "cascade_delete", count, fk.name, depth))
                radius.triggers += catalog.triggers_for(fk.child, "DELETE")
                if depth < self.max_depth:
                    self._walk_delete(cursor, catalog, fk.child, child_rows, depth + 1, path | {fk.child}, radius)
                else:
                    radius.notes.append(f"cascade below {fk.child} not followed (max depth {self.max_depth})")
            elif action in {"n", "d"}:
                effect = "set_null" if action == "n" else "set_default"
                radius.impacts.append(TableImpact(fk.child, effect, count, fk.name, depth))
                radius.triggers += catalog.triggers_for(fk.child, "UPDATE", set(fk.child_cols))
                if action == "n" and fk.not_null_cols:
                    self._predict(radius, "23502",
                                  f"{fk.name} is ON DELETE SET NULL but {fk.child}.{', '.join(fk.not_null_cols)} "
                                  "is NOT NULL")
            else:
                radius.impacts.append(TableImpact(fk.child, "restrict", count, fk.name, depth))
                self._predict(radius, "23503",
                              f"{count} row(s) in {fk.child} still reference {table} through {fk.name} "
                              f"(ON DELETE {FK_ACTIONS.get(action, action)})")

    def _walk_update(self, cursor: Any, catalog: Catalog, table: str, rowset: str, changed: set[str],
                     depth: int, path: set[str], radius: BlastRadius) -> None:
        for fk in catalog.referencing(table):
            if not set(fk.parent_cols) & changed or fk.child in path:
                continue
            child_rows = (f"SELECT * FROM {q(fk.child)} WHERE {_cols(fk.child_cols)} IN "
                          f"(SELECT {', '.join('p' + str(depth) + '.' + q(c) for c in fk.parent_cols)} "
                          f"FROM ({rowset}) AS p{depth})")
            count = self._count(cursor, child_rows)
            if not count:
                continue
            action = fk.on_update
            if action == "c":
                radius.impacts.append(TableImpact(fk.child, "cascade_update", count, fk.name, depth))
                radius.triggers += catalog.triggers_for(fk.child, "UPDATE", set(fk.child_cols))
                if depth < self.max_depth:
                    self._walk_update(cursor, catalog, fk.child, child_rows, set(fk.child_cols), depth + 1,
                                      path | {fk.child}, radius)
            elif action in {"n", "d"}:
                effect = "set_null" if action == "n" else "set_default"
                radius.impacts.append(TableImpact(fk.child, effect, count, fk.name, depth))
                if action == "n" and fk.not_null_cols:
                    self._predict(radius, "23502",
                                  f"{fk.name} is ON UPDATE SET NULL but {fk.child}.{', '.join(fk.not_null_cols)} "
                                  "is NOT NULL")
            else:
                radius.impacts.append(TableImpact(fk.child, "restrict", count, fk.name, depth))
                self._predict(radius, "23503",
                              f"{count} row(s) in {fk.child} still reference the old key through {fk.name} "
                              f"(ON UPDATE {FK_ACTIONS.get(action, action)})")

    def _check_parent(self, cursor: Any, fk: ForeignKey, values: list[exp.Expression], radius: BlastRadius) -> None:
        if any(_is_null(v) for v in values):
            return
        literals = [_literal_sql(v) for v in values]
        if any(lit is None for lit in literals):
            return  # computed values cannot be checked ahead of time
        predicate = " AND ".join(f"{q(col)} = {lit}" for col, lit in zip(fk.parent_cols, literals))
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {q(fk.parent)} WHERE {predicate})")
        if not cursor.fetchone()[0]:
            shown = ", ".join(literals)
            self._predict(radius, "23503", f"{fk.parent} has no row with {', '.join(fk.parent_cols)} = {shown} "
                                           f"({fk.name})")

    # ------------------------------------------------------------- INSERT
    def _preview_insert(self, cursor: Any, catalog: Catalog, statement: exp.Insert, radius: BlastRadius) -> None:
        table = radius.target_table
        source = statement.expression
        radius.triggers += catalog.triggers_for(table, "INSERT")
        if not isinstance(source, exp.Values):
            radius.notes.append("INSERT ... SELECT: row count is only known after execution")
            return
        rows = [row.expressions if isinstance(row, exp.Tuple) else [row] for row in source.expressions]
        radius.direct_rows = len(rows)
        radius.impacts.append(TableImpact(table, "direct_insert", len(rows)))
        cursor.execute(f"SELECT COUNT(*) FROM {q(table)}")
        radius.table_rows = int(cursor.fetchone()[0])
        if not isinstance(statement.this, exp.Schema):
            radius.notes.append("INSERT without a column list: parent and key checks skipped")
            return
        columns = [c.name for c in statement.this.expressions]
        for row in rows:
            values = dict(zip(columns, row))
            for fk in catalog.references_from(table):
                if all(c in values for c in fk.child_cols):
                    self._check_parent(cursor, fk, [values[c] for c in fk.child_cols], radius)
            keys = catalog.primary_keys.get(table, ())
            if keys and all(k in values for k in keys):
                literals = [_literal_sql(values[k]) for k in keys]
                if all(literals):
                    predicate = " AND ".join(f"{q(k)} = {lit}" for k, lit in zip(keys, literals))
                    cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {q(table)} WHERE {predicate})")
                    if cursor.fetchone()[0]:
                        self._predict(radius, "23505",
                                      f"{table} already has a row with {', '.join(keys)} = {', '.join(literals)}")
        sample = [[_literal_sql(v) or v.sql(dialect="postgres") for v in row] for row in rows[: self.sample_rows]]
        radius.sample_columns, radius.sample_rows = columns, sample


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description="Preview the blast radius of one write (read-only).")
    parser.add_argument("sql", help="A single INSERT, UPDATE or DELETE statement")
    parser.add_argument("--json", action="store_true", help="Print the full JSON preview")
    args = parser.parse_args(argv)
    if not has_database_config():
        os.environ["DATABASE_URL"] = DEMO_DATABASE_URL
    radius = BlastRadiusAnalyzer().preview(args.sql)
    if args.json:
        print(json.dumps(radius.to_dict(), indent=2))
    else:
        print(radius.summary())
        for trigger in radius.triggers:
            print(f"  trigger fires: {trigger}")
        for note in radius.notes:
            print(f"  note: {note}")
    return 0 if not radius.error else 1


if __name__ == "__main__":
    sys.exit(main())
