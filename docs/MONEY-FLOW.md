# The money flow, traced from code

Every claim here cites the file and line it was read from. Where a step has NOT been traced, it says so —
an unverified step is marked UNTRACED rather than described, because a map that guesses one leg is worse
than a map with a hole in it, and this document exists because inferring from the database instead of
reading the code produced three wrong answers in one session.

Status: **the LLM realtime CAPTURE → PRICE → RECORD write path (now on `spend_events`, the single money-of-record),
the cap READ path, the conversation/turn + subscription legs, and (2026-09-03) the SERVER-PUSH contract are all
traced. Still UNTRACED: the aggregation/receipt composition details, the batch submit path, and the GPU/remote cap.**

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

## 4. RECORD — `_rt_record` (gate.py 715) → `budget.record_charge` (budget.py 241) → `spend_events`

- in-memory aggregate `_rt_agg`, flushed every 200 calls (`_rt_flush`)
- `_budget_record(...)` (gate.py 480) → `budget.record_charge(provider, model, kind, cost, basis=...)`, with
  `conv_id=QUARANTINE_CONV` when the input exceeded the model's context window (an impossible estimate —
  written for forensics, excluded from totals)

`budget.record_charge` (budget.py 241) then:
- returns early if `is_reading_history()` — re-reading a past batch is not a new charge
- captures `project`, `conv_id`, `key_fp`, and the forensic pair `intent` / `actor` from live context
- **`_record_spend_event(provider, model, kind, cost, …, source="gate")`** (budget.py 273) — the ONE canonical
  write, through `charge_to_event` (budget.py 546) → `SpendLedger.record_event` → `INSERT INTO spend_events`.

### THE WRITE PATH (corrected 2026-09-03 — supersedes the old "charges is the only table" finding)

**`spend_events` is the single money-of-record, and `budget._record_spend_event` (budget.py 103) is its ONE
writer.** Every live path records through it: the gate (`record_charge`, `source="gate"`, budget.py 273),
remote/GPU (`source="remote"`, 493), unpriced markers (704), reconciliation + true-down (769, 812), external
non-LLM (851), the `claudecode` ingest (per-turn est-value `source="claude-code"`; reconciled overage
`source="claude-code-overflow"`; real invoice truth `anthropic-invoice` / `anthropic-invoice-api`), and
`otel_ingest`. `charge_to_event` (budget.py 546) maps each `kind` to ONE of the typed USD columns and sets
`billed` (0 for `est_chat` = plan-covered VALUE, 1 for real money) — so the five categories are separated BY
CONSTRUCTION in the columns, not re-derived by each reader. That closes the old "many stores, many answers"
problem at the root: there is now one store, with typed columns.

The legacy flat `charges` table is **retired**. No live path writes it — the `INSERT INTO charges` this section
used to trace was removed (comments at budget.py 780 / 810 / 832), and a `grep 'FROM charges'` sweep across `src`
is empty (no reader). It survives ONLY as the one-time MIGRATION SOURCE: `migrate_charges.to_spend_events()`
(migrate_charges.py) reads `FROM charges` to rebuild `spend_events` under the exact-Decimal schema
(`spendguard migrate`), proving Σ is preserved across the cutover.

## 5. READ — what a cap counts

`budget.spent_since(day)` (budget.py 591) sums `spend_events` through the ONE countable filter
`SpendLedger._COUNTABLE` (budget.py 35) — NOT the retired `charges` table. `_COUNTABLE` is the single
definition of "money spent in a period"; it replaced the old `countable_charges` VIEW over `charges` (same
rule, one place). It keeps batch + realtime BILLED spend and excludes:
- `kind = 'meta'` — spendguard's own overhead
- synthetic reconciliation / backfill marker rows
- `conv_id = QUARANTINE_CONV` — proven-impossible estimates
- `basis = 'reconstructed'` — a restatement of history, already counted
- est-VALUE (`billed=0`) — never part of a real total (the five-category rule, enforced in the columns)

`budget.exceeded(pending, kind)` checks class-daily, class-monthly, total-daily, total-monthly in order, each
`spent + pending > cap`.

Because the read is now on the SAME typed ledger the write feeds, the "read `charges` directly and get a number
~185x too high" mistake this document was written to stop is structurally gone — one store, one number. The
2026-08-13 OPEN CONTRADICTION (two live answers on one machine, $113 vs $21,009) was a stale process reading
`charges` directly; with `charges` retired and no reader, it cannot recur.

---

## 6. SERVER PUSH — the client→server contract (traced 2026-09-03)

`saas sync` / `saas push` (saas.py) sends this machine's roll-up to the org on TWO SEPARATE AXES that are never
summed:

- **actual-$** → `budget.by_dims()` (budget.py 929) → `saas.build_rollup_rows` (saas.py 302) → `POST /v1/ledger`
  `day_totals`. `by_dims` is ACTUAL-$ ONLY: it sums the BILLED columns (`BILLED_USD_COLS`, never `est_chat_usd`)
  and filters `WHERE COALESCE(billed,1)=1`, so est-VALUE is excluded IN SQL — no est-value can reach the actual-$
  push. `build_rollup_rows` also drops any `est_chat` / `billed=False` row (defense-in-depth, recorded via
  `est_excluded`, never a silent drop), scrubs to the contract fields, and stamps a cross-check `uid` the server
  recomputes (a drift shows up loud, never a silent re-key/double-count).
- **est-VALUE** → a DIFFERENT path: the chat loop (`chat.loop`) + `lane_value.sync` — the client half of the
  server's EST_VALUE_CHANNELS (`claude-code` / `claude-ai` / the ledger-valued gemini·zai plan lanes). It NEVER
  goes to `/v1/ledger`; `saas._is_actual_row` guards the actual-$ crosscheck against ever counting an est-value row.

Overage is reconciled BEFORE the push so actual-$ is factual: the provider INVOICE is the truth and supersedes the
observable overflow ESTIMATE per (provider, month) — the invoiced month's overflow goes `billed=0` (out of the
push, kept for attribution), un-invoiced months keep it `billed=1`
(`claudecode._supersede_overflow_for_invoiced_months`). So the org dashboard receives each real dollar ONCE, on
the correct axis.

---

## UNTRACED — do not treat as described

- ~~**Conversation / turn capture**~~ — **NOW TRACED (2026-09-02) → `claudecode.py`.** `ingest_events`
  (claudecode.py 167) reads `~/.claude` transcripts per-turn (dedup by API `message.id`) and writes one
  `spend_events` row per turn, `source="claude-code"`, `kind="est_chat"` (billed=0 → est-value).
- **Aggregation** — `_rt_flush`, day rollups, and what `report` / `receipt` actually compose
- **The receipt's $591.79** — a third monthly figure, composition unread
- ~~**Server push**~~ — **NOW TRACED (2026-09-03) → §6.** The two-axis `/v1/ledger` contract (actual-$ via
  `by_dims`/`build_rollup_rows`; est-value via chat loop + `lane_value`), and the overage reconciliation that runs
  before it. Still open: how the server forms the org TOTAL from many members' rows.
- **Batch path** — `INTERCEPTORS`, submit gating, and batch reconciliation
- **GPU / remote compute** — `resources.compute_exceeded()` and its own cap
- ~~**Subscription** — fee vs estimated value~~ — **NOW TRACED (2026-09-02).** The real flat fee lands in
  `subscription_usd` via `ingest_invoices` (`source="anthropic-invoice"`, claudecode.py 493); the plan's
  ESTIMATED VALUE is `est_chat_usd` from the per-turn ingest (`kind="est_chat"`, billed=0). Separate columns,
  separate axes — never summed. (Open: how the flat fee is amortized across orgs.)

Each needs the same treatment: read the code, cite the line, state what it does.
