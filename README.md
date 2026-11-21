# NL-to-Action Database Admin Agent

Academic DBMS project for an NL-to-Action database admin agent.

## Project summary

The project is split into an operational database and an agent-system database.

- Week 1: normalized the Online Retail dataset into an operational schema.
- Week 2: added schema metadata and schema lookup.
- Week 3: added a standard-library-only intent parser with session memory, DDL detection, structured constraints, and simple aggregation support.
- Week 4: added LLM-backed SQL/action planning, static SQL validation, and offline evaluation. Generated SQL is not executed.
- Week 5: added fail-closed SQL guardrails, sensitivity checks, row-count thresholds, approval persistence, and an explainable risk classifier. Generated SQL is still not executed.
- Week 6: added a safe execution engine: sandboxed BEGIN/COMMIT/ROLLBACK, pre-commit row and constraint checks, structured capture of execution errors, execution audit and undo (compensation) SQL.
- Week 7: added an end-to-end ReAct agent (reason -> act -> observe) that feeds SQL errors and results back to the planner for bounded retries, with persistent session memory.
- Week 8: added a request-level audit database (intent, schema retrieval, SQL, guardrail verdict, every sandboxed attempt, outcome and response) and a response layer that writes plain-English answers and "why not" root-cause explanations.
- Week 9: added two novelty modules, a plan-before-apply blast-radius preview (foreign-key cascades and predicted constraint failures) and session-level intent drift detection, with an evaluation against ground truth and baselines.

## Repository layout

- `database/schema.sql` - operational PostgreSQL schema.
- `database/sample_data.sql` - real sample rows from the Online Retail CSV.
- `database/sample_data_large.sql`, `database/generate_retail_data.py` - seeded Online Retail-style dataset (2,000 line items in total) and its generator; `setup_demo_db.py` loads it by default (`--dataset sample` for the 13 rows).
- `database/env.py` - reads `.env` (`GROQ_API_KEY`, `LLM_PROVIDER`) for the web app and CLIs.
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
- `database/executor.py` - Week 6 safe execution engine (sandboxed transactions, pre-commit checks, error capture, audit).
- `database/react_agent.py` - Week 7 ReAct agent loop and CLI/REPL.
- `database/session_memory.py` - Week 7 session memory (in-process + `agent_sessions`/`session_events`).
- `database/display.py` - terminal rendering of the ReAct trace, transaction log, checks and results.
- `database/demo_week6_7.py` - end-to-end demo of all Week 6/7 scenarios with a final verification.
- `database/demo_llm.py`, `database/demo_scenarios.json` - offline scripted planner for the demo scenarios.
- `database/setup_demo_db.py` - creates or resets the full demo database without `psql`.
- `database/agent_schema_week6_7.sql` - additive Week 6/7 audit migration.
- `database/agent_schema_week8.sql` - Week 8 audit tables (`agent_requests`, `request_attempts`, `request_explanations`) and the `v_request_audit` view.
- `database/explainer.py` - Week 8 plain-English responses and "why not" root-cause explanations.
- `database/audit_log.py` - Week 8 audit persistence and audit-trail CLI.
- `database/agent_schema_week9.sql` - Week 9 tables (`drift_assessments`, `blast_radius_previews`).
- `database/blast_radius.py` - Week 9 plan-before-apply blast-radius preview.
- `database/intent_drift.py` - Week 9 session-level intent drift detector.
- `database/demo_week8_9.py`, `database/demo_scenarios_week8_9.json` - Week 8/9 end-to-end demo and its scenarios.
- `database/eval_week9.py` - Week 9 evaluation (drift vs labelled sessions, blast radius vs executed ground truth).
- `tests/drift_sessions.json`, `tests/blast_radius_cases.json` - Week 9 evaluation datasets.
- `webapp/server.py`, `webapp/static/` - web app: chat with execution log, and role-based database views (see [`WEBAPP.md`](./WEBAPP.md)).
- `database/rule_planner.py` - offline rule-based SQL planner used when no LLM key is set.
- `database/agent_schema_web.sql` - role-based SQL views for the web app (user, customer agent, database admin).
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

## Weeks 6-7: execute safely, end to end

Details: [`docs/week6_7_notes.md`](./docs/week6_7_notes.md).

### Quick demo (Windows, Docker Desktop running)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo_week6_7.ps1            # run all 14 scenarios
powershell -ExecutionPolicy Bypass -File scripts\demo_week6_7.ps1 -Pause     # one scenario at a time
powershell -ExecutionPolicy Bypass -File scripts\demo_week6_7.ps1 -Interactive  # type your own requests
```

The script starts a PostgreSQL 16 container (`nl2action-pg`, port 55432), creates `.venv`, loads the
schema and sample data, and runs the demo. The demo shows, for every scenario, the
Thought/Action/Observation trace, each statement of the sandboxed transaction, the
pre-commit checks, captured errors and result rows. It ends with a pass/fail table, the
database state proving rolled-back writes changed nothing, and the `execution_audit`,
`compensation_ledger` and `session_events` rows written. A transcript and JSON report are saved in `results/`.

### Manual commands

```powershell
$env:DATABASE_URL = "postgresql://agent:agent@localhost:55432/online_retail_db"
python database/setup_demo_db.py                       # reset schema + sample data
python database/demo_week6_7.py                        # all scenarios (offline scripted planner)
python database/demo_week6_7.py --only W6-7,W7-1       # selected scenarios
python database/react_agent.py -i                      # interactive agent (asks before COMMIT)
python database/react_agent.py "show customers from France"
python database/react_agent.py --dry-run "set the quantity of stock code 85123A on invoice 536365 to 12"
```

Without `GROQ_API_KEY` the agent uses the scripted offline planner, which only knows the
demo requests in `database/demo_scenarios.json`. Set `GROQ_API_KEY` to ask anything; everything
after planning (validation, guardrails, sandboxed execution) is identical in both modes.
In the REPL, `history` prints the session memory and `--session-id <id>` resumes a session.

## Weeks 8-9: audit, explanations and novelty modules

### Pipeline after Week 9

```
NL request
  └─ Week 3 intent parser ── Week 2 schema retrieval (logged: schema_lookup_log)
      └─ Week 4 planner (LLM) + validator   ◄── session memory + execution feedback (Week 7)
          └─ Week 5 guardrails: ALLOW / CONFIRM / BLOCK
              └─ Week 9 blast-radius preview (writes)  ┐  may only escalate
              └─ Week 9 intent-drift check             ┘  ALLOW -> CONFIRM
                  └─ human approval if CONFIRM
                      └─ Week 6 sandboxed execution + pre-commit checks
                          └─ Week 7 ReAct: answer / retry / stop  (records a stop_reason)
                              └─ Week 8 response layer: plain English + "why not" root causes
                                  └─ Week 8 audit: one transaction writes the whole request
```

Both novelty modules run in the same ReAct loop as visible `Action` / `Observation` steps
(`preview_blast_radius(plan)`, `detect_intent_drift(plan, session memory)`).

### Week 8: audit database (`database/agent_schema_week8.sql`)

Every finished request becomes one row in `agent_requests`. Its child rows are written in the
same transaction, on a separate connection after the run. Auditing can never change what the
agent did, and an audit failure is reported as a warning only.

| Table | Kind | Holds |
|---|---|---|
| `agent_requests` | entity, FK to `agent_sessions`, `schema_lookup_log` | request text, parsed intent (JSONB), final SQL + `count_sql`, plan confidence, Week 5 verdict, **effective verdict** after Week 9, risk score, triggered rules, escalations, approval, final status, `stop_reason`, attempts, rows, plain-English response, latency. `UNIQUE (session_id, turn_no)` |
| `schema_lookup_log` | existing Week 1 table, now populated | the top-k tables and scores the planner's prompt was grounded with, and the parsed columns |
| `request_attempts` | associative entity `agent_requests >──< execution_audit` | which sandboxed attempts (Week 6 audit rows) belong to the request. `execution_audit` stays append-only: it is linked, never updated |
| `request_explanations` | weak entity `(request_id, explanation_seq)` | ordered root causes: `stage`, `code`, `headline`, `cause`, `fix`, `evidence` (JSONB) |
| `v_request_audit` | view | one row per request: retrieval, verdicts, number of sandbox runs, last SQLSTATE, outcome, first root cause |

`dbm_writer` gets `SELECT, INSERT` only on the new tables, so the trail is append-only for the
agent's role. `agent_schema.sql` now drops the Week 8/9 tables first, so a reset recreates them
with their foreign keys.

Pipeline stage to where it is recorded:

| Stage | Recorded in |
|---|---|
| intent (Week 3) | `agent_requests.parsed_intent` |
| schema retrieval (Week 2) | `schema_lookup_log` via `agent_requests.lookup_id` |
| SQL (Week 4) | `agent_requests.final_sql`, `count_sql`, `plan_confidence` |
| guardrail verdict (Week 5) | `guardrail_verdict`, `risk_score`, `triggered_rules` |
| novelty checks (Week 9) | `effective_verdict`, `escalations`, `drift_assessments`, `blast_radius_previews` |
| execution result (Week 6) | `request_attempts` -> `execution_audit` (status, SQLSTATE, checks, tx log, txid) |
| ReAct outcome (Week 7) | `final_status`, `stop_reason`, `attempts`, `row_count`, `affected_rows` |
| response (Week 8) | `response_headline`, `response_text`, `request_explanations` |

### Week 8: response layer and "why not" explanations (`database/explainer.py`)

The ReAct loop now records *why* it stopped (`stop_reason`) and a per-attempt log. The response
builder turns that into a headline, a short summary, and, for anything that did not succeed,
ordered root causes with evidence and a suggested fix. It is template-based on purpose: every
sentence comes from facts the pipeline recorded, so an explanation cannot invent a cause (an LLM
paraphrase could).

| `stop_reason` | Response | Root-cause codes (`request_explanations.code`) |
|---|---|---|
| `answered` | "Found 7 line items." / "Here are the results for 5 invoice_no groups." / "The answer is 5." | - |
| `empty_confirmed` | "No customers match your request." | `EMPTY_RESULT` (names the filter that matched nothing) |
| `committed` | "Done: set quantity = 8 on 1 line item in invoice_items." + transaction id + undo SQL | - |
| `planner_block` | "I did not run this: ..." | `PLANNER_DDL`, `PLANNER_UNSCOPED_WRITE`, `PLANNER_UNCLASSIFIED`, `PLANNER_LLM_UNAVAILABLE`, `PLANNER_INVALID_PLAN`, ... |
| `guardrail_block` | rule-by-rule | `WHERE_REQUIRED`, `ROW_LIMIT`, `PII_COLUMN`, `DDL_BLOCK`, `MULTI_STATEMENT`, `WRITE_TAUTOLOGY`, `RISK_CLASSIFIER`, `SQL_VALIDATION` |
| `not_approved` | "This needs a human's approval before I run it." | the CONFIRM rules + `INTENT_DRIFT` / `BLAST_RADIUS_WIDE` escalations + `APPROVAL_NOT_GIVEN` |
| `execution_error` | "PostgreSQL rejected the change; nothing was modified." | `FK_VIOLATION` (parses the referencing table/key), `CHECK_VIOLATION`, `DUPLICATE_KEY`, `NOT_NULL_VIOLATION`, `TRIGGER_REJECTED`, `PERMISSION_DENIED`, `TIMEOUT`, ... |
| `precommit_failed` | "I rolled the change back: it breaks the rule ..." | `INVARIANT_<name>` (quotes the rule and the before/after violation counts), `NOTHING_MATCHED`, `ROW_LIMIT_EXCEEDED`, ... |
| `retry_exhausted` | "I could not produce a query that runs after 3 attempts." | `RETRY_BUDGET_EXHAUSTED` (every attempt as evidence) + the last error |
| `dry_run` / `commit_declined` / `gate_refused` | | `DRY_RUN`, `COMMIT_DECLINED`, `EXECUTION_GATE` |

Notes are added when they matter: which error a retry corrected, the blast-radius preview and
whether execution confirmed it, a drift SHIFT/DRIFT, and the stored undo SQL. If the preview had
already predicted the database error, the root cause says so.

Example (scenario W8-4, deleting a product that is still on an invoice):

```
Action      preview_blast_radius(plan)  [read-only, before applying]
Observation blast radius CONTAINED · would delete 1 row(s) in products (8% of the table) · 38 ms, read-only
            ↳ invoice_items: 1 row(s) still reference the rows (RESTRICT / NO ACTION) via fk_invoice_items_product
            predicted failure 23503: 1 row(s) in invoice_items still reference products through fk_invoice_items_product (ON DELETE RESTRICT)
...
ANSWER  FAILED  PostgreSQL rejected the change; nothing was modified.
  why not 1 ▸ [execution] A foreign-key rule blocked it
      stock_code 84406B is still used by rows in invoice_items. Rule fk_invoice_items_product:
      every line item must point at an existing product, and a product that is still on a line
      item cannot be deleted: ON DELETE RESTRICT. The blast-radius preview had already predicted
      this: 1 row(s) in invoice_items still reference products through fk_invoice_items_product.
      what you can do: Delete or reassign the line items that reference it first.
```

Query the audit trail:

```powershell
python database/audit_log.py --recent 10          # latest requests (from v_request_audit)
python database/audit_log.py --session <id>       # one session, in order
python database/audit_log.py --request 4          # full trail: intent -> retrieval -> SQL -> verdicts -> attempts -> response
python database/audit_log.py --request 4 --json
python database/audit_log.py --why 4              # root causes only
python database/audit_log.py --stats              # outcomes, root causes, escalations, drift levels, preview accuracy
```

### Week 9 novelty module 1: blast-radius preview (`database/blast_radius.py`)

**Problem.** Week 5 judges a write by `count_sql`, which counts only the rows the statement
matches directly. Deleting one invoice "affects 1 row", but `ON DELETE CASCADE` silently deletes
its line items. Deleting a customer nulls `invoices.customer_id` through `ON DELETE SET NULL`.
Deleting a product still on an invoice will fail. None of this is visible until after execution.

**Method.** Before applying a write, the agent previews it inside a `READ ONLY` transaction with
a 3 s statement timeout. The preview reads PostgreSQL's own catalog, so nothing about the schema
is hard-coded:

- `pg_constraint` provides every foreign key with its column lists and `ON DELETE` / `ON UPDATE`
  actions. `pg_trigger` provides user triggers and their events / `UPDATE OF` columns.
- **Direct rows**: rows matched by the write's `WHERE` (or the `VALUES` count), the table size,
  and a sample of the rows.
- **Cascades**: starting from the target rows, the preview walks the FK graph recursively (depth 4,
  with a cycle guard). It builds nested `IN (SELECT ...)` row sets: `CASCADE` -> rows deleted or
  updated in the child; `SET NULL` / `SET DEFAULT` -> rows whose key would change. For `UPDATE`,
  only FKs whose referenced columns are assigned are followed.
- **Predicted failures**: `RESTRICT` / `NO ACTION` references (23503), `SET NULL` on a `NOT NULL`
  column (23502), `INSERT` / `UPDATE` values pointing at a missing parent (23503), and `INSERT` of
  an existing primary key (23505).
- **Spread** (how many distinct parent entities the direct rows belong to) and the **triggers**
  that would fire on every touched table, including cascaded ones.

Classification: **WIDE** if more than 100 rows change in total, or at least 50% of the target
table (more than 1 row), or the cascade depth is 2 or more, or 3 or more tables are touched.
**MODERATE** for any cascade, at least 20% of the table, or more than 10 rows. Otherwise
**CONTAINED**.

**Policy.** WIDE escalates ALLOW to CONFIRM (`BLAST_RADIUS_WIDE`). A predicted failure is reported
but not blocked: the sandbox will fail safely, and running it verifies the prediction. After
execution, `compare()` stores `actual_affected_rows`, `actual_sqlstate` and `prediction_correct`
in `blast_radius_previews`.

```powershell
python database/blast_radius.py "DELETE FROM invoices WHERE invoice_no = '536365'"
# MODERATE: deletes 1 row(s) in invoices (20% of the table); then invoice_items: 7 row(s) deleted by
# ON DELETE CASCADE via fk_invoice_items_invoice; spans 1 customer
```

### Week 9 novelty module 2: session-level intent drift (`database/intent_drift.py`)

**Problem.** Guardrails judge each statement alone. "delete them" after "show the line items of
invoice 537967" becomes a perfectly ordinary, scoped `DELETE ... WHERE invoice_no = '537967'` that
Week 5 allows. Yet the session was only *reading* a moment ago. Ambiguous follow-ups, slips and
injected instructions do their damage exactly at such turns.

**Method.** The detector profiles the last 5 turns of the session from session memory (statement
types, tables, sensitive columns read or written, row counts, request wording). It then scores the
new plan's departure from that profile:

| Feature | Meaning | Weight |
|---|---|---|
| `action_escalation` | `SELECT < INSERT < UPDATE < DELETE`, beyond the most severe statement seen so far | 0.30 |
| `follow_up_flip` | the parser resolved a follow-up ("them", "those") that turns the previous read into a write | 0.20 |
| `table_novelty` | share of the plan's tables the session never used | 0.15 |
| `sensitivity_escalation` | first write (1.0) or first access (0.5) of a HIGH-sensitivity column | 0.15 |
| `scope_escalation` | estimated rows vs the session's median, log10 ratio (100x -> 1.0); uses the blast-radius row count | 0.10 |
| `semantic_shift` | 1 - cosine(request tokens, centroid of earlier requests) | 0.10 |

`score = Σ weight × feature`. Levels: STABLE < 0.25 ≤ SHIFT < 0.45 ≤ DRIFT, and NO_BASELINE for
the first turn. Only a DRIFT on a **write** escalates (`INTENT_DRIFT`). Drift on a read is reported
but not gated, because reads already run READ ONLY. Every assessment is stored in
`drift_assessments`.

**Safety properties shared by both modules.**
- They can only make a verdict stricter: ALLOW -> CONFIRM. They never touch BLOCK and never relax
  CONFIRM.
- When a Week 9 escalation is approved, the executor still receives the original Week 5 verdict.
  The approval therefore does not unlock the larger 1,000-row limit that an approved Week 5
  CONFIRM gets.
- Both run before execution. The preview is read-only, and drift detection needs no database.

### Week 9 evaluation (`python database/eval_week9.py`)

**Intent drift.** `tests/drift_sessions.json` holds 40 sessions (20 drift, 20 stable). They are
labelled by written criteria (anaphoric read->write follow-ups, destructive escalation, first
write of customer identifiers, writes to new tables in a read-only session, 10x scope jumps)
fixed before the detector was run. The decision measured is "escalate".

| Method | Precision | Recall | F1 | False alarms |
|---|---|---|---|---|
| Week 5 per-request guardrails only | - | 0.00 | 0.00 | 0 |
| naive: any write after a read-only history | 0.73 | 0.80 | 0.76 | 6 |
| naive: statement type changed | 0.72 | 0.90 | 0.80 | 7 |
| **drift detector (default weights)** | **1.00** | **0.70** | **0.82** | **0** |

- Ablation (F1 without the feature): `action_escalation` 0.00, `follow_up_flip` 0.57,
  `table_novelty` 0.67, `semantic_shift` 0.71, `sensitivity_escalation` 0.82, `scope_escalation`
  0.82. The last two add nothing on this dataset.
- The 6 misses are destructive escalations inside write workflows (D07, D12, D17) and pure
  scope jumps (D06, D11, D20). Scope carries only 0.10 weight, and Week 5 already confirms or
  blocks writes above 100 / 1,000 rows.
- Threshold sweep: F1 is 0.90 at a DRIFT threshold of 0.25–0.30. That is tuned on these same 40
  cases, so the pre-set default of 0.45 is kept and the sweep is reported, not adopted.

**Blast radius.** `tests/blast_radius_cases.json` has 21 writes against the Week 1 sample data.
Ground truth comes from executing each write in a transaction, diffing full-table snapshots by
primary key (a key change counts as an update), and rolling back.

| Metric | Result |
|---|---|
| per-table row changes predicted exactly (successful in-scope writes) | 13 / 13 |
| constraint-failure prediction (23503 / 23505), in-scope cases | precision 1.00, recall 1.00, SQLSTATE exact 19 / 19 |
| successful writes where the Week 5 count underestimates the true change | 7 / 13 (21 rows Week 5 does not see) |
| out of scope by design | B20 CHECK `23514`, B21 trigger `P0001` (not predicted) |
| preview latency | about 50 ms mean (read-only) |

Each run writes `results/week9_eval_<timestamp>.json` (per-case scores, ablation and sweep).

### Weeks 8-9 demo scenarios (`database/demo_scenarios_week8_9.json`)

Each scenario checks the final status and, where stated, the first root-cause code, the drift
level, the blast-radius level and the predicted SQLSTATE.

| id | shows | expected |
|---|---|---|
| W8-1 | read answered in plain English; full request audited | success |
| W8-2 | grouped result read out as "key: value" pairs | success |
| W8-3 | committed write explained (transaction, undo SQL); first write after reads = drift SHIFT, not escalated | success |
| W8-4 | preview predicts the RESTRICT failure, sandbox confirms 23503, root cause `FK_VIOLATION` | failed |
| W8-5 | Week 5 CONFIRM for customer ids, not approved: `PII_COLUMN` + how to ask safely | needs_approval |
| W8-6 | guardrail BLOCK for `OR 1 = 1`: `WRITE_TAUTOLOGY` | blocked |
| W8-7 | invariant rolls back `quantity = -2`: rule + violation counts 0 -> 1 | rolled_back |
| W8-8 | `truncate` refused by the planner: `PLANNER_DDL` | blocked |
| W8-9 | empty result re-checked once, then explained: `EMPTY_RESULT` | success |
| W9-1 | "delete them" after a read: Week 5 ALLOW, drift DRIFT -> CONFIRM: `INTENT_DRIFT` | needs_approval |
| W9-2 | repeated similar UPDATEs stay STABLE (no false alarm) | success |
| W9-3 | deleting an invoice: preview reports the ON DELETE CASCADE line item; execution confirms it | success |
| W9-4 | 6 rows (50% of the table) is under the Week 5 threshold, preview is WIDE -> CONFIRM: `BLAST_RADIUS_WIDE` | needs_approval |
| W9-5 | preview predicts duplicate key 23505; after approval the sandbox confirms it | failed |

### Run Weeks 8-9

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1              # all 14 scenarios + evaluation
powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1 -Pause       # one scenario at a time
powershell -ExecutionPolicy Bypass -File scripts\demo_week8_9.ps1 -Interactive # your own requests

$env:DATABASE_URL = "postgresql://agent:agent@localhost:55432/online_retail_db"
python database/setup_demo_db.py                # reset: now also applies agent_schema_week8.sql and _week9.sql
python database/demo_week8_9.py                 # all Week 8/9 scenarios (offline scripted planner)
python database/demo_week8_9.py --only W8-4,W9-1
python database/eval_week9.py                   # drift (offline) + blast radius (PostgreSQL)
python database/eval_week9.py --drift-only      # no database needed
python database/react_agent.py -i               # interactive agent, Weeks 6-9 enabled
python database/react_agent.py --no-novelty "..."   # without the Week 9 checks
python database/react_agent.py --no-audit "..."     # without the Week 8 request audit
```

The demo ends with `v_request_audit`, the drift assessments, the blast-radius previews next to
what execution actually did, the root-cause and escalation summary, and one complete trail
(`audit_log.py --request`). A transcript and JSON report are saved in `results/`.
`demo_week6_7.py` runs with the Week 9 checks off so it reproduces Weeks 6–7 exactly. It still
writes the Week 8 audit.

### Week 8-9 changes to earlier code

- `react_agent.py`: records `stop_reason`, a per-attempt log, audit ids, schema retrieval and the
  parsed intent; runs the Week 9 checks between guardrails and approval; builds the response and
  writes the audit before the final `answer` step. All new components are optional constructor
  arguments, so the Week 7 behaviour and tests are unchanged without them.
- `session_memory.py`: turn numbers now continue from the last turn. Previously they were
  `len(turns) + 1`, which repeated after the 20-turn memory cap. They now key the audit rows.
- `demo_llm.py`: the scripted planner also knows the Week 8/9 demo requests.
- `tests/test_guardrails.py`: the "missing row count" test now clears `DATABASE_URL`. With a
  database configured, the guardrail correctly fetches the real count.

### Limitations

- The blast-radius preview predicts FK, NOT NULL-by-SET NULL and primary-key failures. It does not
  evaluate CHECK constraints or trigger logic (B20, B21); those are still caught by the sandbox.
  Rows reached by two cascade paths would be counted twice. `INSERT ... SELECT` row counts are only
  known after execution.
- The preview and the execution are separate transactions under READ COMMITTED, so a concurrent
  change between them can make the prediction stale. The Week 6 pre-commit checks still guard the
  real write.
- Drift weights and thresholds were set by hand before evaluation. The labelled set is small
  (40 cases) and single-annotator, so the numbers are a smoke test, not a benchmark. Drift is
  session-relative: a long, mixed session raises the baseline and dilutes later escalations.
- Explanations are templates over recorded facts. They are reliable but less fluent than an LLM,
  and requests outside the known constraint/rule catalogue fall back to the raw database message.

## Web app

A browser UI for the whole pipeline, with two tabs:

- **Chat**: answers, whether you may modify the database and why, every SQL statement (executed,
  committed, rolled back, rejected, blocked or awaiting approval), and the Thinking / Action /
  Observation execution log.
- **Database views**: three different role views, 5 records per page: a shopper's product catalogue, a support
  agent's customer accounts (masked ids, flags), and a password-protected DBA dashboard (storage, rule health,
  audit trail).

The database holds 2,000 line items by default, and the SQL planner is the Groq LLM when
`GROQ_API_KEY` is in `.env` (offline rule-based planner otherwise).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_webapp.ps1     # then open http://127.0.0.1:8000
```

Complete instructions: [`WEBAPP.md`](./WEBAPP.md).

## Run tests

```bash
pytest
```

The Week 6 integration tests run against `DATABASE_URL` (or the Docker demo database) and
are skipped automatically when PostgreSQL is not reachable. The same applies to the Week 9
blast-radius tests (read-only previews compared with live counts) and the Week 8 audit test,
which runs the real agent on non-modifying requests and deletes its own session afterwards.
Week 8 explainer tests and Week 9 drift tests (including a regression check on the labelled
dataset) need no database.

## Week 1-9 status

| Week | Status | Main output |
| --- | --- | --- |
| Week 1 | Complete | Normalized operational schema, sample data, analysis notes |
| Week 2 | Complete | Schema metadata and schema lookup |
| Week 3 | Complete | Intent parser, session memory, structured constraints, DDL detection |
| Week 4 | Complete | LLM action-plan generation, SQL validation, offline representative-query evaluation |
| Week 5 | Complete | SQL guardrails, row-limit and sensitivity rules, approval persistence, risk classifier and evaluation |
| Week 6 | Complete | Safe execution engine: sandboxed BEGIN/COMMIT/ROLLBACK, pre-commit row/constraint/invariant checks, structured error capture, audit and undo SQL |
| Week 7 | Complete | ReAct reason-act-observe loop, error/result feedback with bounded retries, persistent session memory, end-to-end demo |
| Week 8 | Complete | Audit database (request, retrieval, SQL, verdicts, attempts, response, root causes) + plain-English response and "why not" layer + audit-trail CLI |
| Week 9 | Complete | Novelty modules: blast-radius preview (FK cascades, predicted failures) and session intent drift detection; evaluation with baselines and ablation |

## Notes

- The raw CSV is intentionally not tracked in git; keep it at the repo root or download it separately when loading data.
- Live PostgreSQL validation still requires a local PostgreSQL server and `psql`/connection credentials.