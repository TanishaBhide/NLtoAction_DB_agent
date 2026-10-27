# NL-to-Action Database Admin Agent

Academic DBMS project for an NL-to-Action database admin agent.

## Project summary

The project is split into an operational database and an agent-system database.

- Week 1: normalized the Online Retail dataset into an operational schema.
- Week 2: added schema metadata and schema lookup.
- Week 3: added a standard-library-only intent parser with session memory, DDL detection, structured constraints, and simple aggregation support.
- Week 4: added LLM-backed SQL/action planning, static SQL validation, and offline evaluation. Generated SQL is not executed.
- Week 5: added fail-closed SQL guardrails, sensitivity checks, row-count thresholds, approval persistence, and an explainable risk classifier. Generated SQL is still not executed.

## Repository layout

- `database/schema.sql` - operational PostgreSQL schema.
- `database/sample_data.sql` - real sample rows from the Online Retail CSV.
- `database/load_data.py` - full CSV loader for PostgreSQL.
- `database/agent_schema.sql` - agent-side schema for sessions, policies, lookups, and audit data.
- `database/schema_metadata.json` - lookup metadata for the operational schema.
- `database/schema_retrieval.py` - sparse-term schema lookup engine.
- `database/intent_parser.py` - intent parser with session memory and structured output.
- `database/llm_client.py` - provider-neutral structured tool-calling client and offline mock.
- `database/prompt_builder.py` - schema- and example-grounded prompt construction.
- `database/sql_generator.py` - validated action-plan generator; it never executes SQL.
- `database/sql_validator.py` - PostgreSQL syntax, statement-type, and schema validation.
- `database/few_shot_examples.json` - schema-specific NL-to-SQL examples.
- `database/eval_week4.py` - offline representative-query metrics.
- `database/agent_schema_week5.sql` - additive Week 5 agent-schema migration and rule/sensitivity seeds.
- `database/guardrails.py` - sqlglot-based rule engine and guardrail CLI.
- `database/guardrail_store.py` - parameterized risk-assessment and approval persistence.
- `database/build_guardrail_dataset.py` - deterministic template-derived classifier dataset builder.
- `database/guardrail_dataset.csv` - 240 balanced template-derived classifier examples.
- `database/risk_classifier.py` - logistic-regression training and prediction.
- `database/eval_week5.py` - Week 5 safety and classifier evaluation.
- `database/queries.sql` - example relational-algebra-style SQL queries.
- `tests/` - parser, retrieval, SQL generation, and validation tests.

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

## Generate a Week 4 action plan

Set the provider and Groq API key in the environment; the key is never stored in the repository.
The default model is `openai/gpt-oss-20b` on Groq:

```powershell
$env:LLM_PROVIDER = "groq"
$env:GROQ_API_KEY = "<your-groq-api-key>"
# Optional: override the model identifier
$env:GROQ_MODEL = "openai/gpt-oss-20b"
python database/sql_generator.py "show customers from France"
python database/sql_generator.py "set the price of product with stock code 85123A to 5.00"
python database/sql_generator.py "drop the invoices table"
python database/sql_generator.py --interactive --session-id demo
```

The Groq integration uses the `openai` Python SDK against Groq's OpenAI-compatible API.
To use OpenAI instead, set `LLM_PROVIDER` to `openai` and provide `OPENAI_API_KEY`.
LLM calls have a 30-second default timeout (`LLM_TIMEOUT_SECONDS` can override it)
and at most one SDK retry.

The generator prints a JSON `ActionPlan`. Reads are limited `SELECT` statements; writes
require a `WHERE` on `UPDATE`/`DELETE` and include a matching `count_sql` query. DDL and
unscoped writes are blocked before the LLM call. Nothing is executed against PostgreSQL.
Use `--explain` to request plain PostgreSQL `EXPLAIN` when a database connection is
configured; it does not run `EXPLAIN ANALYZE`.

Inspect schema retrieval, the exact prompt, and (when `GROQ_API_KEY` is configured) the
raw model response with:

```powershell
python database/debug_plan.py "how many invoices per country"
```

Run the deterministic, offline Week 4 fixture evaluation without an API key:

```powershell
python database/eval_week4.py
```

The reported rates describe the 20 curated fixtures, not live-model quality. The same
representative cases can be used for later evaluation.

## Week 5 guardrails

Apply `database/agent_schema_week5.sql` after `database/agent_schema.sql` and the
operational schema are installed. The migration extends the existing plural
`policy_rules` and `column_sensitivity` tables rather than creating duplicate
singular tables.

Build the deterministic training data and classifier:

```powershell
python database/build_guardrail_dataset.py
python database/risk_classifier.py
```

Dataset labels are generated from templates, not human annotations; review them
before using the classifier for real decisions. The dataset is balanced at 80
examples per verdict and records canonical table/column names from this schema.
The stratified split contains examples drawn from the same templates, so its
metrics are a smoke test rather than an independent estimate of production
generalization.

Run guardrails without PostgreSQL access, inspect a plan, or evaluate offline:

```powershell
python database/guardrails.py "show customers from France" --no-db
python database/debug_guardrails.py "how many invoices per country" --no-db
python database/eval_week5.py
python database/eval_week5.py --adversarial
python database/eval_week5.py --adversarial --delay 3 --only B12,B13
```

The offline evaluation prints the unsafe-action blocking rate, safe-query
false-positive rate, verdict confusion matrix, and classifier macro F1; it saves
the results in `database/week5_eval_results.json`.
Adversarial mode requires `tests/adversarial_set.json` and the configured provider
API key. LLM calls time out after 45 seconds by default (`LLM_TIMEOUT_SECONDS`
overrides this); rate-limit responses are retried up to three times. It performs
a 15-second startup health check, caps individual cases at
60 seconds, waits 3 seconds between cases by default, and supports selecting case
IDs with `--only`. It reports unsafe allowed/gated rates, safe-query false
positives, verdict confusion, per-case progress and LLM call latency. LLM errors
are reported separately and excluded from rates. Each run writes a new dated
report under `results/` without replacing an earlier report.

Writes are confirmed above 100 affected rows and blocked above 1,000. These
thresholds are deliberately above the included sample table sizes (customers:
3, products: 13, invoices: 5, invoice items: 13) while still bounding large
bulk changes; they should be recalibrated against production volumes. If a
write's count cannot be obtained, Week 5 requires confirmation. Configured
PostgreSQL access runs only validated read-only count queries.

The rule engine never executes generated SQL. An `ALLOW` is only a decision for
the later execution stage, not execution itself. See
[`docs/confirmation_rules.md`](./docs/confirmation_rules.md) for verdict behavior.

## Run tests

```bash
pytest
```

## Week 1-5 status

| Week | Status | Main output |
| --- | --- | --- |
| Week 1 | Complete | Normalized operational schema, sample data, analysis notes |
| Week 2 | Complete | Schema metadata and schema lookup |
| Week 3 | Complete | Intent parser, session memory, structured constraints, DDL detection |
| Week 4 | Complete | LLM action-plan generation, SQL validation, offline representative-query evaluation |
| Week 5 | Complete | SQL guardrails, row-limit and sensitivity rules, approval persistence, risk classifier and evaluation |

## Notes

- The raw CSV is intentionally not tracked in git; keep it at the repo root or download it separately when loading data.
- Live PostgreSQL validation still requires a local PostgreSQL server and `psql`/connection credentials.