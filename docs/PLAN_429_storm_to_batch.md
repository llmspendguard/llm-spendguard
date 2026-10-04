# PLAN — kill the 429 storm for good: detect bulk-fan scale at submission → divert to Batch

**Status:** active. **Owner contract:** this is DONE only when `tests/test_dispatch_no_429_storm.py` is GREEN 5× in a
row offline AND a live validation run submits a storm-scale fan with **zero surfaced 429s**. Not before, not on anyone's
say-so. Saved here (not in chat) so it is never re-derived — this has been requested ~10×.

## The problem, from OUR ledger (not theory)
8,088 anthropic 429s all-time (vs 44 openai, 16 zai), in repeated storms. Each storm is ONE workload: a bulk
**concurrent fan** (`caller=thread.py:run`, a ThreadPoolExecutor) submitting **thousands of batchable requests at once**:
- 2026-10-02 `honestreview:repo-review` — 3,106 submitted, 3,024 × 429, **peak 4,584 req/min**
- 2026-10-04 `bc-key-symptom-snomed-expression` — 2,368 submitted, 2,285 × 429 (the lmm run)
- 2026-09-28 `honestreview:call-intent` — 2,382 submitted, 1,793 × 429

Anthropic Start-tier opus = **1,000 rpm**. 4,584 req/min is 4.5× over. **A single 429 is a symptom; the disease is
submitting 1000s-at-once to a realtime rate limit.** Per-call pacing cannot fix a 4.5×-over submission — it would just
queue thousands of calls for minutes. The right response is: **at submission, see the scale, divert the whole fan to
the Batch API** (no per-minute storm, ~50% cheaper, and these fans are non-interactive = batchable by definition).

## Root causes (all real, all to fix)
1. **No submission-scale gate.** `bulk_delegate`/the fan admits calls one-by-one; nothing looks at "N=3000 at once vs
   the vendor's rate budget over the window" and decides batch-vs-realtime for the WHOLE fan. (This is the primary fix.)
2. **No proactive rate floor.** The governor starts from "unlimited" and only reacts to a 429; anthropic never learned a
   tpm/rpm (only conn=8). Fixed by the catalog cold cap (`model_catalog.rate_limit_for`, already committed c371927).
3. **Anthropic never learns its ceiling.** `_learn_success_limits` is wired only on the openai branch; the anthropic
   streaming branch never learns tpm/rpm from headers.
4. **No AIMD slow-start on rate.** Acceleration limits 429 a sharp cold burst even below the steady ceiling.
5. **Output-blind pacing.** est_tokens for admission ignores learned expected output (OTPM is real output).
6. **metered_only can shed to a lane under throttle** (billing-pin violation).

## The fix (in dependency order)
- **A. Submission-scale batch-diversion (PRIMARY).** In the fan planner (`whole_job`/`bulk_delegate` via `route_horizon`,
  the C item already built): estimate the fan's aggregate (N requests, Σ input+expected-output tokens) and compare to the
  vendor's realtime budget over the reset window (from the catalog rate limits). If draining realtime would exceed rpm/tpm
  (storm) AND the work is batchable (non-interactive fan) → route the WHOLE fan to the **Batch API** automatically; return
  results when the batch completes. Small/interactive submissions within budget stay realtime.
- **B. Proactive cold cap** (c371927 catalog) applied at `dispatch.admit` so even a non-fan cold burst is paced from call #1.
- **C. Anthropic header-learning** on success + 429 (via `with_raw_response`), so the floor corrects to the real ceiling.
- **D. AIMD slow-start** (additive-increase / multiplicative-decrease — TCP) on the rate window, not only conn.
- **E. Output-aware est_tokens** (feed `expected_output.expect`).
- **F. metered_only never sheds to a lane** under throttle.

## The test — replay the REAL storms, 100% green, repeatable (`tests/test_dispatch_no_429_storm.py`)
Each guard is mechanical, offline, $0, and derived from a mined storm shape — NOT a single synthetic 429:
1. A fan of N=3000 for a batchable intent (the repo-review shape) is DIVERTED TO BATCH at submission (not 3000 realtime).
2. The planner's batch-vs-realtime decision is driven by N × rate vs the catalog budget over the window (not a guess).
3. A storm-scale fan surfaces **ZERO** 429s to the caller (replay of 10-02/10-04/09-28 shapes).
4. A small interactive submission within budget stays realtime (no over-eager batching).
5. Proactive cold cap present for a cold anthropic/opus (non-zero tpm).
6. Anthropic learns tpm/rpm from a success header AND a 429 header.
7. AIMD: multiplicative-decrease on 429, additive-increase on success (bounded).
8. Output-aware est_tokens (intent with large learned output ⇒ larger estimate).
9. metered_only admission never sheds to a lane.
10. Determinism: the suite passes **5 runs in a row** (a loop harness asserts identical GREEN).
Run it 5× via `scripts/probe/run_429_guard_5x.sh`; all 5 must be green.

## Live validation (budget $50–100, estimate-FIRST per the API spend protocol)
After the offline suite is green: submit a real storm-scale fan (sized by a $0 estimate to exceed the realtime budget,
~hundreds of small opus/haiku calls) through the fixed path and assert (a) it auto-diverts to Batch, (b) **zero 429s**,
(c) completes, (d) the billed $ matches the estimate. A SEPARATE $0 estimate run first; proceed only on explicit approval
of the exact number; never cancel a running batch as cost control. Other LLMs/fable may be used for any judging.

## Acceptance (how we KNOW it's done)
- [ ] `tests/test_dispatch_no_429_storm.py` GREEN, 5 consecutive runs.
- [ ] It is in the deploy gate (CI / chunked_suite), so it cannot silently regress.
- [ ] A live storm-scale fan runs with zero surfaced 429s + auto-batch, within the approved budget.
- [ ] A fresh `spendguard doctor`/admission snapshot shows anthropic with a learned/seeded tpm (not conn-only).
