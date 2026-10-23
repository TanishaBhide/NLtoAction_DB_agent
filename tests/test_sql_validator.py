import json
import re
from pathlib import Path

from database.sql_validator import SQLValidator

ROOT = Path(__file__).resolve().parents[1]


def test_accepts_limited_select_with_aliased_tables() -> None:
    result = SQLValidator().validate(
        "SELECT c.country FROM customers AS c JOIN invoices AS i "
        "ON i.customer_id = c.customer_id LIMIT 50",
        "read",
    )

    assert result.valid
    assert set(result.tables) == {"customers", "invoices"}
    assert set(result.columns) == {"country", "customer_id"}


def test_accepts_column_qualified_by_table_alias() -> None:
    result = SQLValidator().validate("SELECT c.country FROM customers AS c LIMIT 10", "read")

    assert result.valid
    assert result.tables == ["customers"]
    assert result.columns == ["country"]


def test_select_alias_is_allowed_in_order_by() -> None:
    result = SQLValidator().validate(
        "SELECT COUNT(*) AS invoice_count FROM invoices "
        "GROUP BY customer_id ORDER BY invoice_count DESC LIMIT 10",
        "read",
    )

    assert result.valid


def test_validates_cte_output_aliases_and_source_columns() -> None:
    result = SQLValidator().validate(
        "WITH country_counts AS ("
        "SELECT country, COUNT(*) AS invoice_count FROM customers GROUP BY country"
        ") SELECT c.country, c.invoice_count FROM country_counts AS c "
        "ORDER BY c.invoice_count DESC LIMIT 10",
        "read",
    )

    assert result.valid
    assert result.tables == ["customers"]


def test_unquoted_uppercase_identifiers_match_postgres_folded_names() -> None:
    result = SQLValidator().validate("SELECT COUNTRY FROM CUSTOMERS LIMIT 10", "read")

    assert result.valid
    assert result.tables == ["customers"]


def test_missing_column_error_names_table_and_suggests_valid_name() -> None:
    result = SQLValidator().validate("SELECT c.country_name FROM customers AS c LIMIT 10", "read")

    assert not result.valid
    assert any(
        "country_name" in error and "customers" in error and "country" in error
        for error in result.errors
    )


def test_rejects_column_that_exists_only_on_another_table() -> None:
    result = SQLValidator().validate("SELECT c.description FROM customers AS c LIMIT 10", "read")

    assert not result.valid
    assert any(
        "description" in error and "customers" in error and "products" in error
        for error in result.errors
    )


def test_rejects_multiple_statements() -> None:
    result = SQLValidator().validate(
        "SELECT country FROM customers LIMIT 1; DELETE FROM customers WHERE customer_id = 'x'",
        "read",
    )

    assert not result.valid
    assert any("exactly one statement" in error for error in result.errors)


def test_rejects_wrong_statement_type_and_unlimited_read() -> None:
    validator = SQLValidator()
    wrong_type = validator.validate("DELETE FROM customers WHERE country = 'France'", "read")
    unlimited = validator.validate("SELECT country FROM customers", "read")
    excessive = validator.validate("SELECT country FROM customers LIMIT 101", "read")

    assert not wrong_type.valid
    assert any("does not match" in error for error in wrong_type.errors)
    assert not unlimited.valid
    assert any("LIMIT" in error for error in unlimited.errors)
    assert not excessive.valid
    assert any("cannot exceed 100" in error for error in excessive.errors)


def test_rejects_unknown_tables_and_columns() -> None:
    validator = SQLValidator()
    unknown_table = validator.validate("SELECT * FROM accounts LIMIT 10", "read")
    unknown_column = validator.validate("SELECT email FROM customers LIMIT 10", "read")

    assert not unknown_table.valid
    assert any("table 'accounts'" in error for error in unknown_table.errors)
    assert not unknown_column.valid
    assert any("email" in error and "customers" in error for error in unknown_column.errors)


def test_rejects_ambiguous_unqualified_columns() -> None:
    result = SQLValidator().validate(
        "SELECT customer_id FROM customers JOIN invoices "
        "ON customers.customer_id = invoices.customer_id LIMIT 10",
        "read",
    )

    assert not result.valid
    assert any("Ambiguous unqualified column" in error for error in result.errors)


def test_requires_where_for_update_and_delete() -> None:
    validator = SQLValidator()
    update = validator.validate("UPDATE customers SET country = 'France'", "write")
    delete = validator.validate("DELETE FROM customers", "write")

    assert not update.valid
    assert not delete.valid
    assert any("must include WHERE" in error for error in update.errors)
    assert any("must include WHERE" in error for error in delete.errors)


def test_validator_recognizes_ddl_statement_type() -> None:
    result = SQLValidator().validate("DROP TABLE invoices", "ddl")

    assert result.valid
    assert result.tables == ["invoices"]


def test_count_sql_must_match_update_filter() -> None:
    validator = SQLValidator()
    result = validator.validate_count_sql(
        "SELECT COUNT(*) FROM customers WHERE country = 'France'",
        "UPDATE customers SET country = 'France' WHERE customer_id = '17850'",
    )

    assert not result.valid
    assert any("same WHERE" in error for error in result.errors)


def test_explain_skips_when_no_database_is_configured(monkeypatch) -> None:
    for name in ("DATABASE_URL", "PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD"):
        monkeypatch.delenv(name, raising=False)
    result = SQLValidator().validate("SELECT country FROM customers LIMIT 1", "read", explain=True)

    assert result.valid
    assert result.explain_message == "EXPLAIN skipped: database connection is not configured."


def test_metadata_tables_and_columns_match_schema_sql() -> None:
    metadata = json.loads((ROOT / "database" / "schema_metadata.json").read_text(encoding="utf-8"))
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    sql_columns: dict[str, set[str]] = {}
    table_pattern = re.compile(
        r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    constraint_names = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}
    for match in table_pattern.finditer(schema_sql):
        table_name, definition = match.groups()
        columns = set()
        for line in definition.splitlines():
            if not line.startswith("    ") or line.startswith("        "):
                continue
            column_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+", line.strip())
            if column_match and column_match.group(1).upper() not in constraint_names:
                columns.add(column_match.group(1))
        sql_columns[table_name] = columns

    metadata_columns = {
        table["name"]: {column["name"] for column in table.get("columns", [])}
        for table in metadata.get("tables", [])
    }
    assert metadata_columns.keys() == sql_columns.keys()
    for table_name, columns in metadata_columns.items():
        assert columns == sql_columns[table_name], table_name
