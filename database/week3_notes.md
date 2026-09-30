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

- `action_type` - `read`, `write`, or `unknown`
- `intent` - a finer label such as `retrieve_records`, `insert_record`, `update_record`, or `delete_record`
- `target_tables`
- `target_columns`
- `constraints`
- `entities`
- `session_context`
- `confidence`
- `matched_examples`

## Why this is enough for Week 3
This week is about intent parsing, not SQL generation. The parser gives later weeks a stable intermediate representation, which is the real handoff point for query generation, safety checks, and transaction execution.

## How to run

```bash
python database/intent_parser.py "show all customers from France"
python database/intent_parser.py "add a new customer from Germany" --session-id S1
```

## Week 3 deliverable status
Done:

- action classification is implemented
- schema-aware entity extraction is implemented
- few-shot examples are present
- session context is preserved in the output
- the parser can be run from the command line
- `pytest` passes with 16/16 tests, covering DDL, read/write cases, aggregation, follow-ups, and retrieval behavior

Still to do later:

- SQL/action generation
- guardrails and policy checks
- transaction handling and concurrency control
- audit logging and compensating actions