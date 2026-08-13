# The money flow, traced from code

Every claim here cites the file and line it was read from. Where a step has NOT been traced, it says so —
an unverified step is marked UNTRACED rather than described, because a map that guesses one leg is worse
than a map with a hole in it, and this document exists because inferring from the database instead of
reading the code produced three wrong answers in one session.

Status: **PARTIAL — the LLM realtime write path and the cap read path are traced. The aggregation,
receipt, server-push and conversation/turn capture legs are NOT.**

---

## 0. The five categories (the model this must implement)

| category | real? | LLM cap | GPU cap | receipt |
|---|---|---|---|---|
| LLM batch spend | real | ✓ | — | ✓ |
| LLM realtime spend (calculated) | real | ✓ | — | ✓ |
| GPU / remote compute | real | — | ✓ | ✓ |
| Subscription fee (flat) | real | — | — | ✓ |
| Subscription **estimated value** | **not real** | never | never | ✓ (separate axis) |

A cap may only ever be evaluated against REAL spend of its own class. Estimated value is never summed with
real money — not in a cap, not in a total, not in a display.

---

## 1. CAPTURE — where a call is intercepted

`gate.install()` (gate.py ~1660) patches, in order:

| table | what it covers | gate.py |
|---|---|---|
| `INTERCEPTORS` | batch submit surfaces | 1510 |
| `RT_INTERCEPTORS` | realtime `create` — openai chat/responses/embeddings, anthropic messages | 1336 |
| `STREAM_INTERCEPTORS` | **streaming helpers** — `Messages.stream`, `Completions.stream`, + async twins | added 2026-08-13 |
| `UNIT_INTERCEPTORS` | non-token billing: images, transcription, TTS, fine-tune jobs | 1493 |
| adapters | litellm / bedrock / vertex, only if already imported | ~1607 |
| `http_capture` | raw-HTTP net, suppressed for SDK traffic to avoid double count | ~1620 |

**Known defect, fixed 2026-08-13.** `STREAM_INTERCEPTORS` did not exist. `adapters._call_once` (adapters.py
~279) calls `c.messages.stream(**kw)` for EVERY anthropic request — a different method from `create`, so it
was never patched. Measured: a call through adapters produced `charges +0`; the identical call through
`messages.create` produced `charges +1`. ~2,921 anthropic calls never reached the ledger; floor on the
missed amount, output tokens only, $21.03. Async twins were missed by the first fix and caught by
`tests/test_every_sdk_surface_that_spends_is_gated.py`.

**A surface in no table is invisible, and nothing reports that.** The guard test above enumerates streaming
helpers on the installed SDKs to close that specific family; other families are still only as covered as
the tables are complete.

## 2. DECIDE — actual usage vs estimate

`_rt_account(model, kw, result, est_fn, act_fn, latency)` (gate.py 913):

- `kw.get("stream")` truthy → the stream wrap FAILED; usage is projected by `_stream_out_estimate`, and the
  row is marked `basis=estimate`. (gate.py 917)
- else `act_fn(result)` → the provider's own usage → `basis=billed`. For anthropic that is
  `_act_anth_msg`, reading `result.usage.input_tokens/output_tokens`.
- `act_fn` returning nothing → falls back to `est_fn(kw)`, `basis=estimate`.

So **`basis` is set by the writer, at the moment of the call, and is the only field that says whether a
number is measured or projected.** Everything downstream depends on it.

## 3. PRICE — `_record_rt` (gate.py ~855)

1. Anthropic input normalisation: `in_for_cost = in_tok + cached`, because Anthropic's `input_tokens`
   EXCLUDES cache reads and `_cost` would otherwise double-subtract and under-bill ~2x.
2. `pricing.realtime_cost(model, in_for_cost, out_tok, cached)`.
3. **Unpriced model** → cost is NOT set to 0. `budget.record_unpriced(...)` writes the tokens with an
   UNPRICED marker, the row is excluded from every total, and the function RETURNS — no charge row. "$0"
   and "we cannot price this" are different claims and only the second is true.
4. **Meta intent** (`spendguard:*`) → `budget.record_meta(...)` and return. spendguard's own overhead is
   tracked apart from workload spend.
5. otherwise → `_rt_record(...)`.

## 4. RECORD — `_rt_record` (gate.py 627) → `budget.record` (budget.py ~830)

- in-memory aggregate `_rt_agg`, flushed every 200 calls (`_rt_flush`)
- `_budget_record(...)` → `budget.record(provider, model, kind, cost, basis=...)`, with
  `conv_id=QUARANTINE_CONV` when the input exceeded the model's context window (an impossible estimate —
  written for forensics, excluded from totals)

`budget.record` (budget.py ~830) then:
- returns early if `not cost`
- returns early if `is_reading_history()` — re-reading a past batch is not a new charge
- captures `project`, `conv_id`, `key_fp`, and the forensic pair `intent` / `actor` from live context
- **`INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id,key_fp,basis,intent,actor)`**
  (budget.py ~862)

### THE STRUCTURAL FINDING

`charges` is the ONLY table the live path writes.

`spend_events` — the hash-chained, richly-typed ledger with `batch_micros` / `realtime_micros` /
`est_chat_micros` / `remote_compute_micros` / `subscription_micros` as SEPARATE COLUMNS, i.e. the schema
that actually implements the five categories above — is written by exactly two things:

- `migrate_charges.to_spend_events()` (migrate_charges.py 31), called by **nothing** outside its own module
- `ledger.SpendLedger` append (ledger.py 354), which the live path never invokes

`budget._ledger()` (budget.py 86) is used ONLY for `.audit(...)` — reattribution and quarantine trails
(budget.py 318, 361, 578).

**So the ledger that separates the five categories by construction is not being fed.** The live path writes
a flat `cost` column plus a `basis` string into the legacy table, and every consumer re-derives the
categories with its own filter. That is the root of the "many stores, many answers" problem, and it is why
per-file review kept passing: each file is correct about its own table.

## 5. READ — what a cap counts

`budget.spent_since(day)` (budget.py ~895) sums `COUNTABLE_VIEW`, not `charges`.

`countable_charges` (budget.py 76) = `SELECT * FROM charges WHERE`:
- `kind != 'meta'` — spendguard's own overhead
- `model NOT IN (markers)` — synthetic reconciliation/backfill rows
- `conv_id != QUARANTINE_CONV` — proven-impossible estimates
- `basis != 'reconstructed'` — a restatement of history, already counted

`budget.exceeded(pending, kind)` (budget.py 891) checks, in order: class-daily, class-monthly, total-daily,
total-monthly, each `spent + pending > cap`.

**Measured 2026-08-13:** `spent_month()` = **$113.08** against a $2,500 cap. Raw `charges` for the same
month = $21,016.89. The view is doing its job; reading `charges` directly gives a number ~185x too high,
which is exactly the mistake this document exists to stop.

### OPEN CONTRADICTION — not explained

A live process (PID 2636, `tools/conversation_digest.py`, running 30 days) logs
`total-monthly spend to $21009.23, over the $2500 cap` and retries in a loop. That figure appears only in
the last 26 lines of a 20,612-line log, so the condition is RECENT. `budget.spent_month()` in a fresh
interpreter returns $113.08. **Two live answers on one machine.** Cause not yet established. Do not trust
either number until it is.

---

## UNTRACED — do not treat as described

- **Conversation / turn capture** — how live turns are read, what is extracted, where it lands
- **Aggregation** — `_rt_flush`, day rollups, and what `report` / `receipt` actually compose
- **The receipt's $591.79** — a third monthly figure, composition unread
- **Server push** — `saas push` / `sync`, what is sent, when, and how the org total is formed
- **Batch path** — `INTERCEPTORS`, submit gating, and batch reconciliation
- **GPU / remote compute** — `resources.compute_exceeded()` and its own cap
- **Subscription** — fee vs estimated value, where each is stored and how they are kept apart

Each needs the same treatment: read the code, cite the line, state what it does.
