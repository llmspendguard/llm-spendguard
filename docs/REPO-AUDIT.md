# Repo audit — every module, every path, traced from code

This is the master audit. It exists because the repo is too large to hold in one context (95 modules,
~27,900 lines), so **the document is the memory**: each pass reads code, records findings with `file:line`
citations, and marks coverage. A claim without a citation is not allowed here — it is the exact failure
(inferring instead of reading) this audit was ordered to end.

## Rules

1. **Code is truth.** Every statement cites the `file:line` it was read from, or is marked `UNVERIFIED`.
2. **One concept, one section.** For each concept: what it CLAIMS (docstring), what it DOES (cited), is it
   implemented ONCE, are there PARALLEL paths, is it WIRED, and the FINDINGS.
3. **Resumable.** The coverage tracker below is the source of truth for what is done. A section is `DONE`
   only when its concept is fully traced end to end.
4. **Findings are logged, not fixed inline.** Fixing mid-audit loses the map. Findings accumulate in
   §FINDINGS; fixes happen in a deliberate pass with their own tests.

## How to resume

Read §COVERAGE, pick the next `UNAUDITED` concept in dependency order (money in → price → record → aggregate
→ cap → display → reconcile → push), trace it from code, append a §concept section, update coverage.

---

## COVERAGE

Status: `DONE` fully traced · `PARTIAL` some paths traced · `UNAUDITED` not yet read this pass.
(Module purposes are each module's own docstring first line — see the inventory at the end.)

### Concepts (the real unit of audit)

| # | concept | status | section |
|---|---|---|---|
| 1 | STORAGE — where spend is stored | **DONE** (this pass) | §1 |
| 2 | CAPTURE — where a call is intercepted | PARTIAL (MONEY-FLOW.md §1) | — |
| 3 | PRICE — tokens → dollars | PARTIAL (MONEY-FLOW.md §3) | — |
| 4 | RECORD — write to the ledger | PARTIAL (MONEY-FLOW.md §4) | — |
| 5 | AGGREGATE — rollups, flush, day totals | UNAUDITED | — |
| 6 | CAP — what enforcement counts | PARTIAL (MONEY-FLOW.md §5) | — |
| 7 | DISPLAY — receipt / report composition | UNAUDITED | — |
| 8 | RECONCILE — ledger vs provider truth | UNAUDITED | — |
| 9 | ADMIN ORACLE — realtime provider truth | PARTIAL (keys + entry verified) | §oracle-keys |
| 10 | PUSH — client → org server | UNAUDITED | — |
| 11 | SUBSCRIPTION — fee vs estimated value | UNAUDITED | — |
| 12 | GPU / remote compute — its own cap | UNAUDITED | — |
| 13 | BATCH — submit gate + reconcile | UNAUDITED | — |
| 14 | THE FIVE CATEGORIES — kept apart everywhere | UNAUDITED (the spine) | — |

---

## §1. STORAGE — where spend is stored  [DONE]

### What exists (19 tables, from `CREATE TABLE` grep over src/)

| table | owner (creator) | role |
|---|---|---|
| **charges** | budget.py | **MONEY OF RECORD — live.** flat rows: `cost` float + `basis` string |
| **spend_events** | ledger.py | **MONEY — intended replacement.** typed columns per category; NOT fed live |
| spend_audit | ledger.py | hash-chained audit trail (reattribution / quarantine) |
| ledger_locks | ledger.py | period-close locks |
| gate_ledger | bulkgate.py | per-sig test+estimate gate state (bulk authorization) |
| gate_calls | bulkgate.py | per-call telemetry (sig, model, out_tok, max_tokens, truncated) |
| gate_latency | bulkgate.py | per-call latency telemetry |
| cost_predictions | calibrate.py | estimate↔actual pairs (calibration) |
| calls | bulkgate.py* | opt-in rich per-call corpus (cost+quality) |
| call_io | callio.py | opt-in prompt/output sample corpus |
| insights / graph_nodes / graph_edges / seg_attribution | learn.py | learning graph |
| model_facts | models.py | per-model verified facts |
| evidence_class / user_ask_class | conv.py | conversation classification |
| savings | guard.py | spend-guarded (cache/block/cascade) accounting |
| semcache | semcache.py | semantic response cache |

\* `calls` table is created in bulkgate.py but written by calls.py — noted for the AGGREGATE pass.

### The finding: TWO money tables, a half-finished migration

- **`charges`** — the live money-of-record. FIVE writers, all in budget.py:
  - `record()` budget.py:174 (INSERT :207) — the main live path, one call → one row
  - `ingest_remote()` budget.py:440 (INSERT :456) — remote GPU box rollup
  - `record_unpriced()` budget.py:604 (INSERT :609) — unpriced-model marker (excluded from totals)
  - `record_reconciled()` budget.py:695 (INSERT :702) — reconciliation gap rows
  - `record_true_down()` budget.py:741 (INSERT :747) — estimate→actual adjustment
  - schema is FLAT: a single `cost` float plus a `basis` string {estimate·billed·assumed·reconstructed}.
    The five spend categories are NOT columns; they are re-derived by every reader via filters
    (`countable_charges` view, budget.py:76).

- **`spend_events`** — the intended replacement. ONE writer: `SpendLedger.append` → INSERT ledger.py:354.
  Its schema HAS the categories as distinct columns (`batch_micros`, `realtime_micros`,
  `remote_compute_micros`, `subscription_micros`, `est_chat_micros`) — i.e. it is the table that would make
  "keep the five categories apart" structural instead of filter-derived.
  - `migrate_charges.to_spend_events()` (migrate_charges.py:31) is the ONLY thing that populates it, and
    its docstring calls itself a "One-time migration: the legacy charges ledger → the fin[ancial]
    spend_events." **Grep shows no caller of `to_spend_events` outside its own module.** (verify each
    consumer next pass)
  - `budget._ledger()` (budget.py:86) constructs a SpendLedger but is used ONLY for `.audit(...)` trails
    (budget.py:318, 361, 578) — never `.append(...)`. So the live path never writes spend_events.

### Verdict

- **Implemented once? NO.** Two money-of-record tables exist. `charges` is live; `spend_events` is a built
  but un-cut-over replacement. This is the archetype "concept implemented twice / parallel path."
- **Wired? PARTIAL.** `charges` is fully wired. `spend_events` is wired to a migration that nothing calls
  and to reads that need enumeration (dashboard push? report? — UNVERIFIED, next pass).
- **Consequence:** every monthly total is re-derived from a flat table by a per-consumer filter, which is
  the mechanism behind the multiple conflicting monthly figures ($21,016.89 raw charges / $113.08 the
  cap's `countable_charges` / $0.65 spend_events / $591.79 receipt). Same month, four numbers, because the
  category split lives in filters, not in the schema that was built to hold it.

### Next actions (logged, not done)

- [ ] Enumerate every READER of `charges` and of `spend_events` — confirm which totals come from which
      table (DISPLAY + RECONCILE + PUSH passes).
- [ ] Decide the ONE money table. Either finish the migration (cut readers to `spend_events`, the typed
      schema) or formally retire `spend_events` and make `charges`+view the sanctioned single source. This
      is a design decision for the owner, not an audit call — but the two-table state must not persist.

---

## §oracle-keys — ADMIN ORACLE key resolution  [verified]

- Oracle: `realtime_oracle.py`. Reads `OPENAI_ADMIN_KEY` (:43) and `ANTHROPIC_ADMIN_KEY` (:63) via
  `config.api_key()`. Runs only when `SPENDGUARD_ADMIN_ORACLE` is set (`ledger_sync.py:731`).
- `config.api_key(name)` (config.py) resolves: `os.environ` → `$SPENDGUARD_ENV` → `./.env` →
  `~/.spendguard/keys.env` → `~/.spendguard/.env`; first hit wins.
- Both keys resolve (OpenAI 133 chars, Anthropic 110 chars). Copied into `keys.env` (the cwd-independent
  primary) 2026-08-13. Admin usage endpoints are READ-ONLY (org usage reports) — zero spend.

---

## FINDINGS (accumulating)

| id | severity | finding | status |
|---|---|---|---|
| F1 | high | capture leak: SDK streaming helpers ungated (anthropic + openai, sync+async) | **FIXED** `6228638` |
| F2 | high | two money tables; `spend_events` migration built, never cut over | OPEN (§1) |
| F3 | med | multiple conflicting monthly totals — root is F2 (filter-derived categories) | OPEN |
| F4 | med | PID 2636 computes $21k monthly live while `spent_month()`=$113.08 — cause not established | OPEN |
| F5 | low | `estimate` fn shadowed the `estimate` module (import-order dependent) | FIXED `776caa1` |

---

## MODULE INVENTORY (95 modules — declared purpose = own docstring line 1)

Regenerate with: `python -c "import ast,pathlib; [print(p.stem, ast.get_docstring(ast.parse(p.read_text()))[:80] if ast.get_docstring(ast.parse(p.read_text())) else '') for p in sorted(pathlib.Path('src/spendguard').glob('*.py'))]"`

Full list captured in the audit run 2026-08-13; the largest 40 by LOC are the logic-bearing ones and are
prioritised in the concept passes above. Every module maps to at least one concept row in §COVERAGE; a
module not yet reached by a concept pass is `UNAUDITED` by definition.
