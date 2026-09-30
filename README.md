# NL-to-Action Database Admin Agent

Academic DBMS project for an NL-to-Action database admin agent.

## Project summary

The project is split into an operational database and an agent-system database.

- Week 1: normalized the Online Retail dataset into an operational schema.
- Week 2: added schema metadata and schema lookup.
- Week 3: added a standard-library-only intent parser with session memory, DDL detection, structured constraints, and simple aggregation support.

## Repository layout

- `database/schema.sql` - operational PostgreSQL schema.
- `database/sample_data.sql` - real sample rows from the Online Retail CSV.
- `database/load_data.py` - full CSV loader for PostgreSQL.
- `database/agent_schema.sql` - agent-side schema for sessions, policies, lookups, and audit data.
- `database/schema_metadata.json` - lookup metadata for the operational schema.
- `database/schema_retrieval.py` - sparse-term schema lookup engine.
- `database/intent_parser.py` - intent parser with session memory and structured output.
- `database/queries.sql` - example relational-algebra-style SQL queries.
- `tests/` - parser and retrieval tests.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Put `database_raw.csv` at the repo root, or pass a path to the loader.

3. Set one of the following connection options:

- `DATABASE_URL=postgresql://user:password@host:5432/dbname`
- or `PGUSER`, `PGPASSWORD`, `PGHOST`, `PGPORT`, `PGDATABASE`

## Load the data

```bash
python database/load_data.py --reset
```

The loader stages the CSV, cleans the data according to the Week 1 rules, and loads the normalized tables inside one transaction.

## Run the parser

```bash
python database/intent_parser.py "show customers from France"
python database/intent_parser.py "drop the invoices table"
python database/intent_parser.py --interactive --session-id demo
```

## Run schema lookup

```bash
python database/schema_retrieval.py "customer invoice country"
```

## Run tests

```bash
pytest
```

## Week 1-3 status

| Week | Status | Main output |
| --- | --- | --- |
| Week 1 | Complete | Normalized operational schema, sample data, analysis notes |
| Week 2 | Complete | Schema metadata and schema lookup |
| Week 3 | Complete | Intent parser, session memory, structured constraints, DDL detection |

## Notes

- The raw CSV is intentionally not tracked in git; keep it at the repo root or download it separately when loading data.
- Live PostgreSQL validation still requires a local PostgreSQL server and `psql`/connection credentials.