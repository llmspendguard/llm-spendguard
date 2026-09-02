# Handoff: make Claude Code **conversation-level** spend a first-class tracked dimension

**Date:** 2026-09-01 · **Origin:** cost-crisis investigation. Weekly plan cap was exhausted; Claude Code
turns began overflowing onto pay-as-you-go API credit. spendguard reported ~$0 because it only sees
**gated** calls — it never saw the Claude Code *app's own turns*, which are where the money actually went.
A throwaway attributor (`scripts/conversation_cost_attributor.py`) reconstructed per-conversation cost
directly from Claude Code transcripts and immediately located the four "whale" sessions burning ~$136/hr
of est-value. This handoff turns that throwaway into a tracked spendguard capability.

## GROUNDED UPDATE (2026-09-01) — build this as an EXTENSION; most of §1–2 already ships
Grounded against the code + the live ledger before building (repo rule #0). Findings:
- **§1 and most of §2 already exist.** `claudecode.py` (`spendguard claude-code`) mines ~/.claude transcripts
  INCREMENTALLY (per-session line/mtime watermark), dedups by **API `message.id`** across resume/branch/compaction
  replays, prices per-model realtime with an honest un-lumped cache-read split, and reports per-model value.
  `conv.py` adds segmentation + agentic attribution (`accounting` = provider usage → project via conversations).
  Do NOT port the throwaway — it is a THIRD pricing path that already disagrees with the shipped one by ~9%
  ($34.5K vs $37.6K all-time est-value). Extend `claudecode.py`; keep only its title-join idea, then delete it.
- **The actual blind spot (verified).** `spend_events` (the queryable ledger) has **0 claude-code rows and 0
  `from_message_ids`**. App turns live only in a JSON state file + a server push, ALL flagged `billed=False`.
  While the weekly plan is active that is correct; once the weekly cap is exhausted those same turns overflow to
  real API $ and are STILL booked `billed=False` and STILL absent from the ledger → "$0 in ledger while the
  balance drops." That is exactly why post-cap token burn was untraceable.
- **ZERO new columns needed.** `spend_events` already has `conv_id` (indexed, 99.6% populated), `seg_id`,
  `cache_read_tok`/`cache_write_tok`/`reasoning_tok`, `dedup_key`, `from_message_ids`, and the billed/est split as
  SEPARATE typed columns (`billed`, `subscription_usd`, `realtime_usd`, `cost_type`, `eligibility_window`,
  `window_start`, `reconciled`, `gap_flag`). So:
  → `context_tokens` = `in_tok+cache_read_tok+cache_write_tok` (DERIVED, no column).
  → `billing_state` = `cost_type`+`billed`+`subscription_usd`/`realtime_usd` (DERIVED view, no column).
  → conversation title = resolved at READ time from `conv_id` via the session store (titles change — don't
     denormalize a stale copy into the ledger).
- **Net-new work = a new SOURCE + a reconciliation pass + read views**, not a schema change: (1) write claude-code
  turns into `spend_events` (`source="claude-code"`); (2) **OBSERVABLE overage detection** → billing_state (read the
  transcript's own `quotaLimits.isUsingOverage` / rejected-cap-hit signals — never a guessed cap, never admin);
  (3) context trajectory + compaction signal; (4) read-time title join. Revised §2b/§3 below reflect this.

## The gap this closes
- The Anthropic **admin/billing API cannot attribute below `api_key_id`** — no conversation/session id
  exists anywhere in usage or cost reports. Conversation-level truth lives **only** in local transcripts.
- spendguard's ledger currently records only calls routed through the gate. The **Claude Code application
  itself** (this and every other open chat) is an untracked spender. When the plan cap overflows, that
  untracked spend becomes **real billed dollars** with no attribution — exactly the blind spot that caused
  the "$0 in ledger but balance dropping" confusion.

## Ground truth: where the data is
- Transcripts: `~/.claude/projects/<project-slug>/<session-uuid>.jsonl`, one JSON object per line.
- Every assistant message carries `message.usage`: `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens` (cache **write**), `cache_read_input_tokens` (cache **read**),
  plus `message.model` and a top-level ISO `timestamp`.
- **Sidebar title (what the user recognizes)**: NOT in the transcript. It lives in the desktop app's
  session store: `~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json`. Each record
  has `cliSessionId` (== the transcript filename uuid) and `title` (plus `titleSource`, `lastActivityAt`).
  Join transcript `<uuid>` → `title` via `cliSessionId`. A transcript can have several session json files
  (resumes/bridges); pick the one with the greatest `lastActivityAt`. Fallbacks only if no record exists:
  transcript `type:"summary"` line, then first genuine `type:"user"` message.
  Note: `ccd_session_mgmt` `sessionId` (`local_<uuid>`) is a DIFFERENT namespace from the transcript uuid —
  do not join on it directly; go through `cliSessionId` in the json store above.
- **Model can change within a conversation.** Price EACH message at `message.model` (already done) and
  report cost **per model** per conversation (e.g. `opus-4-8 $X + opus-5 $Y + fable-5 $Z`), never a single
  "current model" — the single-model label is misleading for any long session that switched models.

## Pricing — from spendguard only, never hardcoded
Use `spendguard.pricing.cost_or_unpriced(model, in_tok, out_tok, cached_in_tok=<cache_read>,
cache_creation_tok=<cache_write>, batch=False)`. Claude Code is interactive → **`batch=False`**.
Cache multipliers already live in pricing (`CACHE_READ_MULTIPLIER=0.1`, `CACHE_WRITE_5M_MULTIPLIER=1.25`).

## What to build

### 1. `spendguard conversations` subcommand (+ library fn)
Port `scripts/conversation_cost_attributor.py` into the package as a proper CLI + importable function.
Output: top conversations by spend in a `--window-min` window **and** all-time, each labeled by
transcript title, model, last-active, token breakdown. Keep the `--top`, `--window-min` flags.
Follow the repo's **name-uniqueness** rule — adjudicate the module/function names against the registry
before adding them (the throwaway name `conversation_cost_attributor` is already unique and semantic; keep
or rename deliberately, don't collide with existing `estimate`/`cost`/`record` families).

### 2. Ingest transcript usage into the ledger as a tracked source
Add an ingester that writes one ledger row per assistant message with:
- `executor = "claude-code"`, `provider = anthropic`, `model = message.model`, `kind` = `realtime`
  (the app is not batch), token fields mapped as above, `cost` from `cost_or_unpriced(batch=False)`.
- **New attribution dimensions**: `conversation_id` (transcript uuid) and `conversation_title`. If the
  ledger has no such columns, add them (nullable) rather than overloading `chain`/`intent`.
- `project` = the transcript's project slug / cwd.
- **Billing state must be queryable (the whole point of the DB ingest).** Reuse the ledger's existing
  subscription-vs-billed `kind` semantics: each ingested turn carries a resolvable billing-state that
  reconciliation (§3) fills — `plan_covered` (subscription active, $0 real) vs `billed_overflow` (weekly
  cap exhausted, real dollars). Then "what did conversation X cost when the subscription was NOT active"
  is a single `SUM(cost) WHERE conversation_id=? AND billing_state='billed_overflow'`. Store both the
  est-value cost (always) and the real-billed flag so the two are never conflated in one column.
- **Idempotent**: dedup by a stable per-message key (message `id` + session uuid). Re-running must not
  double-count. Follow the CHUNK doctrine — incremental `--since <ts>` / checkpoint the last-ingested
  offset per file; one malformed line must not wedge the run.
- Pure parse + arithmetic → **$0, no LLM, no gate needed**. If you later add LLM enrichment
  (e.g. topic classification of a conversation), that call runs **under the gate** and is agentic, never regex.

### 2b. Record context size per turn (drives recurring cost) + compaction signal
The dominant cost of a long conversation is **cache-read = the context re-read every turn**. Record it:
- Per assistant message, store `context_tokens = input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens` (≈ the prompt/context size fed that turn). Add a `context_tokens` column.
- Per conversation, expose the **context trajectory**: current/last context size, max, and mean; plus
  `recurring_read_cost_per_turn` = cost attributable to `cache_read` at the current context size.
- Emit a **compaction signal** when a conversation sustains a large context (e.g. context_tokens above a
  configurable fraction of the model's `CONTEXT_LIMITS` for N consecutive turns): "conversation X is
  running at ~Y tokens/turn, costing ~$Z/turn in context re-read; compacting would cut it ~k×." This is a
  DECISION about whether a session is bloated — keep the *threshold* configurable, but if you later
  classify "is this context still useful," that judgement is agentic (LLM), never a hardcoded rule.
- Why it matters to the subscription question: while the plan is active this re-read is $0; once the
  weekly cap overflows, sustained large context is precisely what bleeds the API balance. Tracking it lets
  spendguard name the expensive-to-keep-alive open sessions.

**How (the note — do it this way):** every ingested claude-code turn already carries
`context_tokens = in_tok + cache_read_tok + cache_write_tok`, so the per-conversation **trajectory**
(current/last, max, mean, slope) is just a QUERY over the claude-code rows grouped by `conv_id` ordered by `ts` —
no per-turn table, no new column. Per-turn **recurring cost** = `cache_read_tok × realtime cache-read rate(model)`
= what you pay every turn merely to re-read the retained context (this is the dominant cost of a long session).
The compaction **signal** is a cheap CONFIG pre-filter: a conversation holding
`context_tokens > compaction_frac × CONTEXT_LIMITS[model]` for `N` consecutive turns (both `compaction_frac` and
`N` configurable) emits `"conv X: ~Y tok/turn ≈ $Z/turn in context re-read; compacting cuts it ~k×"`. Do NOT
hardcode `k` — **MEASURE** the real before/after ratio from sessions that already compacted (Claude Code leaves a
`type:"summary"` marker in the transcript, so the true post-compaction context size is observable). The threshold
only decides WHICH sessions are worth a closer look; the actual judgement — *"is this retained context still
earning its per-turn cost, or should we compact / close it?"* — is an **agentic LLM call under the gate reading the
session's recent content, never a hardcoded rule** (a big context that is still actively used is not waste; only an
LLM can tell the difference).

### 3. Split billed $ from est-value — SEGMENT the timeline into subscription vs overage WINDOWS
Est-value and real-paid apply to DIFFERENT time windows, and that split is the whole point. Est-value is the correct
frame ONLY while we are UNDER the subscription; once past the weekly limit and running on credits, those are REAL PAID
tokens (the API). So classify a turn by WHICH WINDOW it falls in — never by a cap number we invent, never by the admin
API. Segment each timeline (per conversation, and the account) from the transcript's OWN observable limit signals:
  • SUBSCRIPTION window [reset → weekly-cap hit): usage is PLAN-COVERED → est-value (est_chat, billed=0).
  • OVERAGE window [weekly-cap hit → next reset), credits available: successful turns run on the API → REAL PAID
    tokens (realtime, billed=1) — the money spent OUTSIDE the subscription.
  • BLOCKED: inside an overage window with no credits (out_of_credits / org-disabled), attempts are REJECTED → $0
    (friction, not paid). Rejected attempts carry no usage, so they never count as spend.
  • RESET: the weekly allowance refreshes → back to a subscription window.
Boundaries are OBSERVED: the cap-hit (B) = a `seven_day` quotaLimits record / the 429 "You've hit your weekly limit"
message; the reset (C) = `resetsAt` (or the "resets <date>" text). RECLASSIFY, don't re-ingest — a turn in an overage
window MOVES from est_chat to a realtime/billed row (source=claude-code-overflow); the est-value stream is
subscription-only, the paid stream overage-only. CROSS-CHECK the paid total against the real auto-recharge invoices —
the overage est-value is an UPPER BOUND that reconciles DOWN to the actual billed $ (the admin usage_report is
org-API-key scoped and does NOT see Claude Code subscription overage, so it is a partial cross-check only). Always
render `Real $X (overage, reconciled) :: Est-value $Y (plan-covered)` — never one summed number.
CAUTION learned the hard way: `quotaLimits.isUsingOverage` appears ONLY on REJECTION records (always false here), so
it MISSES successful overage — the correct signal is "successful turns that continue PAST a weekly-cap hit until
reset". Measured that way: usage ran past the wall Aug 17–Sep 2 (~$3.4K est-value of overage turns, upper bound).

### 4. (Optional) `--watch` / lightweight daemon
Tail active transcripts and surface a live per-conversation $/min, so a runaway open session is visible
immediately instead of discovered via a dropping balance.

## Verification gate (do not skip)
Before calling it done: run the staged validation the repo requires — attribute a known day, cross-check
the summed est-value against Admin `cost_report` for the same window per api_key, and confirm the ledger
ingest is idempotent (re-run → identical totals). Show the receipt.

## Reference implementation
`scripts/conversation_cost_attributor.py` in this repo is the working throwaway (read-only, prices from
`spendguard.pricing`, labels by transcript summary/first-user line). Treat it as the spec, not the product.
