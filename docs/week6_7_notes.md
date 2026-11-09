# Weeks 6–7: Safe execution engine and end-to-end agentic loop

## Pipeline

```
NL request
  └─ Week 3 intent parser (session-aware follow-ups)
      └─ Week 4 planner (LLM) + static SQL validator     ◄── session memory + execution feedback (Week 7)
          └─ Week 5 guardrails: ALLOW / CONFIRM / BLOCK
              └─ Week 6 SafeExecutor: sandboxed transaction + pre-commit checks
                  └─ observation ──► Week 7 ReAct controller decides: answer, retry, or stop
```

## Week 6: `database/executor.py` (`SafeExecutor`)

**Execution gate (before any connection is opened).** The plan must not be
blocked. The verdict must be ALLOW, or CONFIRM with explicit approval. The SQL
is re-parsed and must be exactly one statement whose type matches the plan:
SELECT for reads, INSERT/UPDATE/DELETE for writes. DDL, transaction control and
raw commands are refused.

**Sandboxed transaction.** The executor issues the transaction control itself
(autocommit is off at the protocol level, so every statement is visible in the log):

| Step | Read | Write |
|---|---|---|
| `BEGIN ISOLATION LEVEL READ COMMITTED …` | `READ ONLY` | `READ WRITE` |
| `SET LOCAL statement_timeout / lock_timeout` | 5 s / 2 s | 5 s / 2 s |
| snapshot | — | `count_sql` estimate, invariant counts, before-image |
| `SAVEPOINT agent_write` | — | ✓ |
| statement | the SELECT (max 100 rows fetched) | the write + `RETURNING *` |
| `SET CONSTRAINTS ALL IMMEDIATE` | — | forces deferred constraints to fire now |
| pre-commit checks | — | see below |
| end | always `ROLLBACK` (nothing to commit) | `COMMIT` only if all checks pass, not a dry run, and the commit decider agrees |

The READ ONLY mode is real defence in depth. `SELECT nextval(...)` passes the
static gate but PostgreSQL rejects it with SQLSTATE 25006 (tested).

**Pre-commit row/constraint checks** (all must pass, otherwise `ROLLBACK`):

| Check | Rule |
|---|---|
| `deferred_constraints` | `SET CONSTRAINTS ALL IMMEDIATE` succeeds |
| `rows_affected_nonzero` | a write that matched nothing is not committed |
| `rows_match_estimate` | affected rows = `count_sql` estimate taken in the same transaction (or the VALUES row count for INSERT) |
| `row_limit` | ≤ 100 rows, or ≤ 1000 when a CONFIRM verdict was approved (the Week 5 thresholds) |
| `invariant:invoices_keep_line_items` | no invoice is left without line items |
| `invariant:negative_qty_only_on_cancellations` | negative quantity only on cancelled invoices |
| `invariant:negative_price_only_on_adjustments` | negative unit price only on adjustment invoices |
| `invariant:status_matches_invoice_prefix` | C/A invoice-number prefix agrees with status |

Invariants compare violation counts **before vs after** the write, so rows
that already violated a rule do not block unrelated writes. They cover
business rules the schema does not enforce. For example, PostgreSQL accepts
`quantity = -3`, but the invariant check rolls it back.

**Error capture.** Every database error becomes an `ExecutionError` with
SQLSTATE, error class, message, detail, hint, constraint, table and column. It
is classified by SQLSTATE class:

| Class | Category | Retryable by the agent? |
|---|---|---|
| 42 | syntax_or_semantic (bad type/operator/column) | yes |
| 22 | data_exception (bad literal format) | yes |
| 40, 55 | transaction conflict / lock timeout | yes |
| 23 | integrity_violation (CHECK, FK, UNIQUE, NOT NULL) | **no**: the request breaks a rule |
| P0 | business_rule_trigger | **no** |
| 57 | statement timeout / cancelled | no |
| 42501 | permission | **no**: never retry around permissions |

**Audit.** Every attempt, including refusals and rollbacks, is written to
`execution_audit` on a *separate* connection, so the audit row survives the
rollback it describes. It records the status, SQLSTATE, check results, the full
transaction log, txid, attempt number and duration. Committed writes also
store generated **undo SQL** in `compensation_ledger`: the inverse UPDATE, the
re-INSERT of deleted rows, or a DELETE of inserted rows, built from the
before-image or the `RETURNING` rows. The undo SQL is stored and never run
automatically.

## Week 7: `database/react_agent.py` (`ReActAgent`)

Each request runs a bounded **Thought → Action → Observation** loop
(`max_attempts = 3`). The actions are `generate_sql`, `check_guardrails`,
`request_human_approval` and `execute_sandboxed`. The controller is a
deterministic policy and the LLM is used only for planning. Safety decisions
(stop, retry, approval) are therefore never delegated to the model.

| Observation | Next thought / action |
|---|---|
| retryable SQL error | feed the error (SQLSTATE, message, hint, failing SQL) back to the planner and retry |
| read returned 0 rows | one result-feedback retry (e.g. case-sensitive LIKE → ILIKE); same SQL again = accepted |
| constraint / trigger error | stop and explain; data unchanged |
| pre-commit check failed | stop and explain; the plan was right, the request breaks a data rule |
| guardrail BLOCK or planner block | stop: **the agent never retries around a safety block** |
| CONFIRM | ask a human; not executed without approval |
| attempts exhausted | stop with "retry budget exhausted" |

**Session memory** (`database/session_memory.py`):
- every finished turn (request, final SQL, status, rows, attempts, error,
  first rows) is kept, and the last 3 turns are given to the planner as a
  `SESSION MEMORY` block;
- the Week 3 parser resolves follow-ups ("only the ones with quantity above 6")
  against the previous turn in the same session;
- memory is durable: `agent_sessions.current_state` (JSONB) holds the summary
  and `session_events` stores every thought/action/observation/answer as
  `(session_id, event_seq)`. `--session-id` resumes a session across runs.

## Offline vs live planner

`database/demo_llm.py` is a scripted stand-in for the LLM. It covers the 15
demo requests and returns attempt *N* after *N* failed attempts, so the retry
path can be shown without an API key. Everything else is real: the parser,
validator, guardrails, executor and PostgreSQL. With `GROQ_API_KEY` set,
`react_agent.py` uses the live model and accepts any request.

## Demo scenarios (`database/demo_scenarios.json`)

| id | shows | expected |
|---|---|---|
| W6-1 | read in READ ONLY sandbox | success |
| W6-2 | committed UPDATE, 8 checks pass, undo SQL stored | success |
| W6-3 | dry run: executed then rolled back | rolled_back |
| W6-4 | CHECK violation 23514 captured | failed |
| W6-5 | trigger rejection P0001 captured | failed |
| W6-6 | FK RESTRICT violation 23503 captured | failed |
| W6-7 | invariant check rolls back `quantity = -3` | rolled_back |
| W6-8 | invariant check: invoice would lose all line items | rolled_back |
| W6-9 | CONFIRM → approval → committed INSERT | success |
| W6-10 | DDL blocked, never reaches the DB | blocked |
| W7-1 | SQL error (42883) fed back → corrected on attempt 2 | success |
| W7-2 | empty result fed back → ILIKE on attempt 2 | success |
| W7-3 | three failures → bounded stop | failed |
| W7-4 | follow-up resolved from session memory | success |

## Tests

`tests/test_executor.py` contains unit tests for error classification and the
gate, plus integration tests against PostgreSQL. The integration tests are
skipped automatically when no database is reachable, and they undo their own
commits. `tests/test_react_agent.py` covers retry, stop, bound, approval and
memory behaviour with fakes, plus prompt-feedback wiring.
