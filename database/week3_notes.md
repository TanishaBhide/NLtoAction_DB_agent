# Week 3 Natural-Language Intent Layer

## Goal
Week 3 turns schema retrieval into a structured understanding layer. The agent now takes a user request, classifies the action, extracts the target schema objects, identifies constraints, and preserves session context.

## What was added

- `intent_examples.json` with small few-shot examples for read and write requests.
- `intent_parser.py`, a dependency-free intent parser that uses:
  - keyword-based action classification,
  - schema lookup for table suggestions,
  - schema metadata for column suggestions,
  - simple constraint extraction,
  - session context passthrough,
  - a structured `ParsedIntent` output.

## Output shape
The parser returns a structured object containing:

- `action_type` - `read`, `write`, `ddl`, or `unknown`
- `intent` - a finer label such as `retrieve_records`, `aggregate_count`, `aggregate_sum`, `aggregate_avg`, `insert_record`, `update_record`, `delete_record`, `cancel_invoice`, `drop_table`, `truncate_table`, `alter_schema`
- `target_tables`
- `target_columns`
- `constraints` - structured `{table, column, operator, value}` objects
- `set_values` - column/value pairs for updates
- `aggregation`, `order_by`, `limit`
- `is_destructive`, `is_unscoped` - flags for the Week 5 guardrails
- `resolved_from_context`, `previous_request` - follow-up handling
- `entities`
- `session_context`
- `confidence`, `ambiguities`
- `matched_examples`

## Why this is enough for Week 3
This week is about intent parsing, not SQL generation. The parser gives later weeks a stable intermediate representation, which is the real handoff point for query generation, safety checks, and transaction execution.

## How to run

```bash
python database/intent_parser.py "show all customers from France"
python database/intent_parser.py "add a new customer from Germany" --session-id S1
```

## Classification rules (in priority order)

1. Questions about past changes are reads: a question form ("how many", "which", "list", "show", ...) plus a past-tense change word or did/were/was/has been. "how many invoices did we delete last month" is a read, not a delete. An imperative destructive verb at the start ("delete invoices that were cancelled") is never treated as a question.
2. Updates use one shared rule: set/change/update/modify/make `<column>` ... to `<value>`, including "change customer 13047's country to Italy" and "update unit price to 3 for stock code 85123A". The new value goes into `set_values` and is never also added as a WHERE filter.
3. Weak destructive verbs ("empty", "clear") count only as the main imperative verb, so "show products with an empty description" stays a read.
4. DDL keywords (drop, truncate, alter, grant, revoke, create table).
5. Destructive synonyms (delete, remove, get rid of, erase, purge, wipe, clear out).
6. Remaining read/write cues, then schema nouns and known values.

Constraints: one comparison rule for every numeric column (`quantity`, `unit_price`, with price/cost/priced as aliases), date ranges, before/after, year ("in/from/for/during 2011"), this/last month and year, `invoices.status` (cancelled / adjustment / completed), country, invoice number, stock code, customer id, and description ILIKE.

Aggregation: sum / count / avg with GROUP BY from "per / by / for each / in each" (country, customer, product, invoice, status, month, year), plus top-N.

Follow-ups in the same session inherit tables, aggregation and constraints. A new constraint on the same column replaces the old one ("now only the ones from Germany"); a constraint on a different column is added ("only for customer 17850", "same but for 2011", "only cancelled ones").

## Evaluation

- `pytest`: 60 tests pass (intent parser, schema lookup, loader cleaning, held-out report).
- Held-out round 1 (`tests/heldout_cases.json`, 15 sentences written before the parser changes): 6/15 before the changes, 15/15 after. The rules were designed after seeing these failures, so 15/15 overstates how well the parser generalises.
- Held-out round 2 (`tests/heldout_cases_round2.json`, 15 new sentences written after the changes and run once without tuning): 14/15 on the first run. The miss was U13, the follow-up "only cancelled ones": the status word was ignored because the sentence doesn't say "invoice". It was fixed afterwards by applying status words when the previous request in the session was about invoices, and it now passes (15/15 after the fix).
- Run `pytest tests/test_heldout.py -s` to print both pass rates.

Known limits (keyword/regex parser, no LLM until Week 4): relative dates beyond this/last month and year, multi-condition OR logic, negation ("customers not in France"), and joins described in free text are not parsed.

## Week 3 deliverable status
Done:

- action classification is implemented, including DDL, destructive synonyms and questions about past changes
- schema-aware entity extraction is implemented
- structured constraints, `set_values` for updates, and aggregation with GROUP BY
- few-shot examples are present
- session memory with follow-up merge/replace rules
- the parser can be run from the command line (`--session-id`, `--interactive`)
- `pytest` passes with 60/60 tests, plus two held-out evaluation sets

Still to do later:

- SQL/action generation
- guardrails and policy checks
- transaction handling and concurrency control
- audit logging and compensating actions