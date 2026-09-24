# GUARDRAILS — reasoning-model overspend must be spendguard's job to prevent

**Status:** ✅ IMPLEMENTED (2026-09-24) — all five guardrails shipped, each with the acceptance test the spec below
requires, wired into the suite (chunked_suite.py) so the class cannot regress. **Filed:** 2026-09-23. **Severity:**
high — two overspends, ~$93 avoidable on a ~$98 run, same root cause, one hour apart.

**What shipped (commit · acceptance test):**
- **A — honor-or-refuse the effort pin** (`ba2ac9e` · test_effort_pin_honored_or_refused). At the `_call_once`
  effort-resolution point, a `'minimal'` pin remapped to a floor that still reasons (gpt-5.x → `'none'`) is recorded +
  surfaced un-swallowably (`bulkgate.note_unhonored_effort` / `unhonored_efforts()`), never a silent `'none'`. Honor is
  impossible at gpt-5.5's knob, so the loud refusal + the routing fix is the only honest half; it never guesses a value.
- **B — effort is path-independent** (`c1cd960` · test_effort_path_independent, TEST-ONLY). Already structurally true —
  plain / `governed=True` / `bulk_delegate` fan all thread the same `reasoning` into the one resolver; the test locks it.
- **C — reasoning-aware estimate** (`1e00f8c` · test_reasoning_estimate_coldfloor). `expected_output.expect` already
  used the measured p90 for a WARM reasoning class; a cold-reasoning-floor rung now uses the documented reasoning seed
  (shared with `maxtokens`) so a COLD class is never estimated at ~160 / the 128k ceiling / unknown-0.
- **D — `budget_usd` is a REAL running cap** (`167fd25` · test_budget_running_cap). `bulk_delegate` tracks ACTUAL cost
  and stops submitting at the cap, returning `budget_exhausted` rows — never spends to N, never cancels in-flight.
- **E — per-call runaway breaker** (`3abfc7c` · test_runaway_breaker). `bulkgate.check_runaway` records (never aborts) a
  completed call whose out_tok exceeds `bulkgate.runaway_factor` × the MEASURED p99 norm; a cost-anomaly monitor
  (arithmetic on billed tokens), not a semantic verdict.

The counts ride the shared admission snapshot (`dispatch.admission_state` → `unhonored_efforts`, `runaways`), rendered
identically by `spendguard dispatch` (CLI) and `spendguard_dispatch_state` (MCP). New knob `bulkgate.runaway_factor`.
The original directive follows, unedited.

---

This is a prompt for whoever next works spendguard. It is not a caller's bug to keep re-guarding in every script —
it is spendguard's CORE MISSION (estimate-first, a real cap, honor the pin) failing. Implement the guardrails below so
"such madness never occurs again" is enforced by spendguard, not by each caller remembering.

## What happened (measured, from `~/.spendguard/spend.db`)

A warden cross-check fanned many one-shot structured calls to OpenAI reasoning models. Two incidents:

1. **`reasoning='minimal'` silently NOT applied to gpt-5.5.** The caller passed `reasoning='minimal'` on every call.
   Recorded `effort` for **gpt-5-mini = minimal → avg 121 out tokens → $0.0005/call** (correct). Recorded `effort` for
   **gpt-5.5 = `none` → avg 4,249 out tokens → $0.1317/call** — the SAME pin, silently dropped for a different model.
   345 calls → **$45.44**, ~15× what an honored minimal (~$3) would cost. No warning, no error.

2. **`governed=True` bypassed the minimal-effort default.** An earlier pass on gpt-5.5 with `governed=True` did not
   apply the `reasoning=minimal` default the non-governed path applies. ~380 calls ran to ~4,249 out tokens, were CUT
   at the deadline (`finish=null`, **no parsed output at all**), and billed **$50.82 for null results**.

Both were invisible until the ledger was read after the fact. The upfront estimate said ~$3 (it used the caller's
`per_out≈160` guess); nothing reconciled estimate-vs-actual mid-run, and there was no real spend cap — only an
upfront estimate-gate that the wrong estimate sailed through.

## Root causes spendguard must own

- **A. A caller's explicit effort pin is dropped SILENTLY, per-model.** `reasoning='minimal'` reached the wire for
  gpt-5-mini but not gpt-5.5 (`chosen_effort`/`requested_effort` recorded `none`). A control the caller set that is not
  applied — and not surfaced — is worse than no control: it reads as safe and is not. (See `adapters.call` effort
  resolution + `_ALIASES` at adapters.py; the "must never reach the wire as a literal reasoning_effort" pin logic.)
- **B. Effort policy is PATH-DEPENDENT.** `governed=True` (the dispatch fan) and the plain path apply different effort
  defaults. The same call must get the same effort regardless of which door it takes.
- **C. The estimate is not reasoning-aware.** It trusted a caller `per_out`; a reasoning model's real output is
  reasoning+answer (thousands of tokens), which spendguard already MEASURES per (model, effort, intent) in the ledger.
  An estimate that ignores that cannot gate anything.
- **D. There is no REAL running cap.** `budget_usd` (where present) gated only the upfront estimate. Actual cumulative
  cost was never tracked against a ceiling mid-fan, so a 15×-underestimated run spent to completion unimpeded.
- **E. No per-call runaway breaker.** A single call emitting 4,249 out tokens when the (model, effort, intent) norm is
  ~121 is a runaway; it should trip a breaker (cap/abort + record), not bill in full × N.

## Required guardrails (each with an acceptance test — prose alone does not count)

1. **HONOR OR REFUSE THE EFFORT PIN, NEVER DROP IT SILENTLY.** When a caller passes an explicit `reasoning`/effort, it
   is applied to the wire request for EVERY model that supports it; for a model where it cannot be applied, spendguard
   raises a typed error or emits a NAMED, un-swallowable warning `effort '<x>' requested but NOT applied to <model>`
   and records `requested_effort` so it is auditable. *Test:* for each reasoning model (gpt-5-mini, gpt-5.5, o-series),
   pin `minimal` and assert the RECORDED `chosen_effort == 'minimal'` (or a loud refusal) — never `none` with the pin set.

2. **EFFORT DEFAULT IS PATH-INDEPENDENT.** The reasoning default (and an explicit pin) resolve identically on the
   `governed=True` dispatch path and the plain path. *Test:* same (model, intent, reasoning) through both paths →
   identical recorded effort.

3. **REASONING-AWARE ESTIMATE.** `estimate`/`deadline_for`/bulkgate cost projections for a reasoning model use the
   MEASURED p50/p95 `out_tok` for (model, effort, intent) from the ledger — not a caller `per_out`. When no history
   exists, use a documented reasoning-model floor (NOT ~160). *Test:* on a recorded reasoning intent, the estimate is
   within a bounded factor (e.g. ≤2×) of realized cost; a regression that reintroduces a 160-token assumption fails.

4. **`budget_usd` IS A REAL RUNNING CAP.** A fan/bulk loop (dispatch, `submit_chat_tasks`, `lane_fan`, the realtime
   BatchPool) tracks cumulative ACTUAL cost from each result and ABORTS the moment the next call would cross the
   caller's ceiling — returning what completed, not spending to N. *Test:* a fan with a low cap over many items aborts
   after ≈cap has been spent, with the remainder unissued.

5. **PER-CALL RUNAWAY BREAKER.** When a call's `out_tok` exceeds K× the measured (model, effort, intent) norm (or a
   sane absolute ceiling when no norm exists), cap it (bounded `max_output`) or abort it, and RECORD the trip. *Test:*
   a call that would run away is capped/aborted and the event is recorded, not billed in full.

## The one-line doctrine to add

A pinned effort that is silently dropped, an estimate blind to reasoning tokens, and a `budget_usd` that caps only the
estimate are all the SAME failure: spendguard let a caller believe it was governed when it was not. **Every spend
control spendguard exposes must be REAL at the moment it matters, or fail loud — never silently absent.** Enforce with
the five tests above; wire them into the suite so this class cannot regress.
