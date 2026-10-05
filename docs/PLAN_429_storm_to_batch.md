# PLAN — kill the 429 storm for good: detect bulk-fan scale at submission → divert to Batch

**Status:** active. **Owner contract:** this is DONE only when the ⭐ acceptance test
`tests/test_incident_replay_storm.py` (pace+batch COMBO, control-arm has teeth) is GREEN offline AND the full
`docs/REQUIRED_TESTS_429_storm.md` set passes AND a live validation run submits a storm-scale fan with **zero surfaced
429s**. Not before, not on anyone's say-so. Saved here (not in chat) so it is never re-derived — this has been requested
~10×. **The acceptance substrate exists** (`tests/storm_harness.py` §0 FakeProvider+collector+canary;
`tests/test_incident_replay_storm.py` the ⭐ replay) and is correctly RED on the COMBO assertions (5 & 6) by
construction — it is the contract the fix below must turn green, not a green that pretends.

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

## ROOT CAUSE CONFIRMED — the burst-allowance hole (2026-10-04, proven in code + replay)
Traced end-to-end via `tests/test_incident_replay_storm.py` + `tests/storm_harness.py`. The metered fan
(`lane_balance._run_task_on_api` → `dispatch.acquire_or_none` → `_Bucket.acquire`) DOES carry rpm/tpm pacing, and the
bucket is seeded correctly (`vendor:anthropic → conn=8, rpm=2400, tpm=2M`). Yet a 300-burst thrashed a 50/s wall **931
times**. The cause is one line — `_Bucket.__init__`:
```python
self._tokens = float(self.rpm)   # start full so the first burst up to rpm is not paced
```
The RPM limiter is a **token bucket that starts full**. A burst ≤ rpm is admitted entirely UNPACED (every
`_rpm_wait_s()` returns 0), leaving only `conn=8` — which, with fast responses, cycles far above the wall. **At real
scale this IS the incident**: anthropic rpm=1000, bucket starts with 1000 tokens → the first ~1000 of 4,584 arrivals
are admitted unpaced → stampede the 1,000-rpm wall → ~8,088 429s. This is correct for a lone interactive call (don't
delay it) but catastrophic for a storm — so **per-request admission structurally cannot catch a storm; the burst is
admitted before pacing engages.** Only coalesce-then-plan (see below) sees the cohort in time.

Second confirmed defect — **N:N not guaranteed**: the re-admit loop's only outcomes are "got a slot" or a TERMINAL
signaled miss `"queue full: no slot within {deadline}s"` (`lane_balance.py:888`). There is NO branch that reroutes
un-admittable overflow to batch, so tail items (observed: CANARY-115, -201 of 300) time out instead of being delivered.
The "queue full" miss must become "divert to batch / re-enqueue", never terminal.

## THE DESIGN (Ash, 2026-10-04) — COALESCE then PLAN the cohort (async; the wait is the enabler)
The contract is ASYNC: the caller submits N (1 or thousands) and awaits N results through one handle; realtime and
batch are interchangeable paths UNDER the door; no 429 ever surfaces. The fix is NOT more per-request cleverness (the
burst-allowance hole makes that impossible) — it is:
1. **COALESCE** at the one submission seam into a cohort cut at a LOGICAL BREAK (adaptive, not a fixed 1s): accumulate
   arrivals while watching an idle-gap timer (~10ms). Each new arrival RESETS the gap (debounce), so a cohort is cut
   only at a sensible boundary — a "work set that makes sense to work against", never mid-stream while the queue is
   still filling. Fire the cohort on the FIRST of: (a) **quiescence** — no arrival within the idle gap (the burst has
   landed); (b) **max-wait ceiling** (~1s) — a sustained trickle never blocks forever; cut it, process, open a new
   window; (c) **early storm** — the accumulated cohort already exceeds the realtime sustainable budget, so it is
   provably a storm; stop waiting and plan+divert now. N=1 / small steady traffic hits quiescence almost immediately
   → fired fast, never needlessly delayed. The wait is not a cost — it is what lets the planner SEE the whole storm and
   chunk batch properly.
2. **PLAN the cohort**: `sustainable = (rpm/60) × horizon`; serve that share realtime by REAL pacing, divert the
   `N − sustainable` overflow to the Batch API (reuse `queue_planner.plan_batch_chunks`). The COMBO — pace the
   sustainable share AND batch the overflow — not batch-only (delays what realtime could serve now) and not
   pacing-only (unbounded backlog = the storm).
3. **RETURN through the same async handles**: unbundle batch results back to each request. "queue full" is eliminated
   because the overflow has a home → N:N guaranteed, zero thrash, zero surfaced 429.

## WHERE THE COALESCER ATTACHES (grounded 2026-10-04 — the "one place")
Chokepoint map: egress is single (`adapters._call_once` ← `_call_guarded`). Admission has TWO doors into the SAME
per-vendor `_Bucket`: single/metered calls via `adapters.call`/`vendor_call` → `dispatch.admit`
(adapters.py:1064, vendor_call.py:427); the fan via `lane_balance` → `dispatch.acquire_or_none` (lane_balance.py:882,
958). The real incident was MANY concurrent `adapters.call`s (honestreview's own ThreadPoolExecutor), not one
`bulk_delegate` — those requests converge ONLY at the shared admission door, each on its own thread. And `admit` only
GATES (returns a token); it does not EXECUTE, so it cannot "divert to batch" by itself.
⇒ The coalescer is a SUBMISSION LAYER at that common door that owns execution + results: every call enters it; it
accumulates the cohort (adaptive logical-break above); per cohort it runs the COMBO (sustainable share → existing paced
realtime via admit+`_call_guarded`; overflow → Batch API); and it FULFILLS each request's future from whichever path
served it. The calling thread blocks on its future — sync API preserved, async underneath. REUSE
`queue_planner.plan_batch_chunks`, `submit_chat_tasks`/`collect_chat_tasks`, `batch_tracker`, `lane_queue` — not a
rebuild (CLAUDE.md #0: extend, don't rebuild).

CONCRETE SEAM (grounded in adapters.py:1053-1116): there is ALREADY a dormant `route_through_queue` hook — a LABELLED
synchronous call (`intent/sig` set, not `_no_guard`/`_probe`) can be recorded through `lane_queue` and run via its
normal path, guarded against re-entry by `_route=False` (the same flag `bulk_delegate` sets so already-queued work
does not open a second row). The coalescer attaches HERE: when coalescing is enabled for the vendor and the call is
not coalescer-internal, the request joins the vendor's cohort (`StormCoalescer.submit`) and the caller awaits its
future INSTEAD of `admit`+`_call_guarded` inline. Both storm shapes pass through this one seam (a lone concurrent
`adapters.call` AND `bulk_delegate`'s per-task `adapters.call`). The coalescer's injected `execute_realtime` re-enters
`adapters.call` with `_route=False` (+ governed) so `dispatch.admit` still paces as a SECONDARY guard and there is no
recursion. Feature-flagged (like `route_through_queue`, default OFF) → land + prove the ⭐ test green BEFORE turning it
on anywhere.

## INVARIANT → TEST MAP (Prompt 8 discipline — no property without a proving test)
Legend: ✅ exists+green · 🔴 exists+RED-by-contract · ✍️ to write THIS build (coalescer unit) · ⏳ later phase.
Unit tests = `tests/test_storm_coalescer.py` (offline, deterministic, drives the coalescer in isolation against
`tests/storm_harness.FakeProvider`). E2e = `tests/test_incident_replay_storm.py`. A–K refer to `REQUIRED_TESTS_429_storm.md`.

| # | Invariant (must hold at scale) | Proving test | Status |
|---|---|---|---|
| I1 | N submitted → N returned; EACH future resolved exactly once (none pending, none duplicated) — N∈{0,1,2,300,1000}, incl. concurrent submit | unit `::conservation` ; e2e #3 | ✍️ / 🔴 |
| I2 | Demux: future for request i resolves with the result FOR i, across BOTH realtime+batch paths and chunk boundaries (canary bijection) | unit `::bijection` ; e2e #4 | ✍️ / 🔴 |
| I4 | ZERO surfaced 429/raw rate-limit to the caller — a realtime 429 is absorbed → re-routed to batch, never surfaced | unit `::realtime_429_reroutes_to_batch` ; e2e #2 ; H3 | ✍️ / 🔴 / ⏳ |
| I5 | Realtime egress ≤ sustainable rate, PACED by the coalescer's own pacer (NOT the burst-allowance bucket) | unit `::realtime_rate_bounded` (measured inter-release) ; e2e #5 | ✍️ / 🔴 |
| I6 | COMBO under pressure: realtime share 0<x≤budget AND overflow→batch (≥1 job, batch_served ≥ N−budget) | unit `::combo_split` ; e2e #5+#6 | ✍️ / 🔴 |
| I7 | Below threshold: a small cohort within budget → realtime only, ZERO batch jobs (no over-eager batching) | unit `::small_cohort_stays_realtime` ; B2 | ✍️ |
| I8 | A burst arriving within the idle gap forms ONE cohort, planned once (O(1)), not per-request | unit `::coalesces_into_one_cohort` (cohorts==1) ; F5 | ✍️ |
| I9 | Logical-break cut: quiescence / max-wait / early-storm; a lone call fires at ~idle_gap, never needlessly delayed | unit `::logical_break_timing` | ✍️ |
| I10 | ONE chokepoint: every submission enters the coalescer; nothing bypasses | e2e C1/C6 (egress==admit) + wiring test | ⏳ |
| I11 | Batch returns short (partial) → the missing items are re-routed; final count still N (replaces today's terminal "queue full" miss) | unit `::partial_batch_refilled` ; A6 | ✍️ |
| I12 | A routing exception resolves the affected futures with a typed error (never leaves one pending); honest degrade | unit `::no_future_left_pending_on_error` | ✍️ |
| I13 | Crash mid-batch → restart reattaches the in-flight batch, all N returned, no double-submit | G1 SIGKILL (durable `lane_queue`) | ⏳ |
| I14 | Batch chunks respect provider caps; union == overflow; no overlap | unit `::chunks_cover_overflow_exactly` ; B5 | ✍️ |

Mechanisms OPENED (Prompt 8): (a) the RPM `_Bucket` starts full → cannot pace a burst → the coalescer MUST own its
realtime pacer (I5). (b) `submit.submit_chat_tasks` is OpenAI-ONLY (refuses non-OpenAI) → the batch seam must be
provider-aware (openai→submit_chat_tasks; anthropic→Message-Batch path) → coalescer takes an INJECTED `execute_batch`
so it stays pure and offline-testable; production wires the provider dispatch. (c) `plan_batch_chunks` returns `chunks`
as a list of int COUNTS (verified) → slice overflow by count, never drop the tail (I14).

## PANEL FINDINGS — Prompt 9 adversarial review (2026-10-04; opus-5 / gpt-5.6-sol / gemini-3-pro / kimi; glm timed out)
The cross-vendor panel reviewed the map above and CONVERGED on holes one mind missed. Folded in as new invariants +
re-classifications. This is the Prompt 9 gate working: a missing invariant caught here costs a map row, not an incident.

**Re-classified ⏳ → 🚫 BLOCKER (unanimous — deferring silently breaks the contract):**
- **I10 one-chokepoint wiring** — the incident was concurrent `adapters.call`, and there are TWO admission doors
  (`dispatch.admit` AND `dispatch.acquire_or_none`). If either bypasses the coalescer the full-bucket stampede returns.
  "A well-tested dead letter" until wired. BLOCKER before any "done".
- **I13 idempotency / SIGKILL** — batch runs for MINUTES; in-memory futures + no idempotency key ⇒ a restart drops
  every diverted request AND double-submits/double-bills on resume. Violates "always returned" + "never cancel a batch".
- **CROSS-PROCESS shared cap (NEW — was not on the map; the single most important)** — `_Pacer` is per-process; K
  processes each pace to 100% of the vendor cap ⇒ K× the wall ⇒ the storm returns, harder to diagnose. The real
  incident WAS uncoordinated concurrency. Needs a cross-process shared lease/token budget (durable store / lane_queue).

**New invariants to ADD to the map (union of the panel):**
| # | Invariant | Assertion | Layer |
|---|---|---|---|
| I15 | **Batch demux BY ID, not position** | shuffled/missing/duplicate/unknown batch results → each future gets ONLY the result carrying ITS id; positional `zip` is never used (TODAY'S `_run_batch` has this data-corruption bug) | unit |
| I16 | **Deadline → typed backpressure, never a minutes-long block** | a request whose batch ETA > its deadline resolves with a typed `DeadlineExceeded` BEFORE the deadline — never blocks the caller thread for the batch's minutes/hours (sync→async thread-exhaustion deadlock) | unit+integration |
| I17 | **Token/multi-window rate safety** | realtime share = MIN(rpm budget, tpm budget / est_tok_per_req); over ANY sliding 1s sub-window releases ≤ per-second cap (not just rpm averaged / 60) | unit |
| I18 | **Batch API's OWN limits** | no `execute_batch` call exceeds the provider's per-batch request/byte/token cap; concurrent in-flight batches ≤ provider max; submission/poll QPS governed → the batch lane itself never 429s/413s | unit+integration |
| I19 | **Poison-request isolation** | one malformed/erroring item in a cohort fails only ITS future (typed), siblings still succeed — no whole-group abort (TODAY `_run_batch` except-clause fails the whole group; I12 encodes this bug) | unit |
| I20 | **Cohort reservation across the horizon** | realtime capacity reserved within the horizon is shared across OVERLAPPING cohorts — a sustained stream cannot be split into budget-sized cohorts each routed entirely realtime (backlog, no overflow) | unit |
| I21 | **Success ≠ a resolved error** | after ACCEPTANCE the governor owes eventual success or durable recovery; a translated `{"error":...}` does NOT satisfy N:N. Typed rejection is allowed only BEFORE acceptance (explicit acceptance boundary) | contract |
| I22 | **Durable disposition ledger** | every request's terminal disposition (realtime/batch/error + idempotency key + provider job id) is recorded durably so Σ dispositions == N survives a restart and reconciles with provider usage | integration |
| I23 | **Governor fails CLOSED** | a pacer/planner/limiter failure never falls back to ungoverned direct realtime egress | unit |
| I24 | **metered_only never sheds to a $0 lane** under throttle (the lmm defect, as an invariant) | unit |
| I25 | **Economics: cheapest feasible lane** | when batch ETA ≤ deadline, overflow prefers batch; a $0 lane is chosen over paid realtime when it satisfies | unit |

**Proxy-test weaknesses to FIX in the existing unit suite (the panel's test-of-the-test):**
- I1: `cap=100000` makes the wall unreachable + `submit_all` is SERIAL → proves "resolves under zero contention", not concurrent-submit conservation. → add a CONCURRENT-submit arm under a real wall.
- I5: `elapsed>1.4` is loose (25% fast still passes) and can't see intra-window bursting. → assert a sliding 1s-window max ≤ per-second cap from release timestamps.
- I9: `lone<0.3` has NO lower bound. → assert `idle_gap ≤ lone < max_wait`, and add max-wait + early-storm arms.
- I11: the fake drops the TAIL → encodes the positional bug. → fake drops a MIDDLE id + shuffles (drives I15); assert bijection holds and only the missing id refills; add refill-depth-exhaustion arm.
- I12: asserts whole-group error → encodes the poison bug. → split into I19 (one item errors, siblings succeed).
- I14: proves coverage not cap-legality; only OpenAI. → add per-chunk byte/token/request-cap legality (drives I18).
- Test discovery: the `::name` selectors are print-labels, not pytest nodes (repo runs test_runner.py subprocess-style) — keep the convention but the map must reference the CHECK LABELS, not pytest `::` ids.

**Design reframe the panel forces (the next build):** the coalescer's rate budget + accumulator + disposition state must
move from in-process memory to the DURABLE, cross-process `lane_queue` (sqlite), with idempotency keys on batch submit,
ID-keyed demux, token-aware + sliding-window pacing, deadline-aware routing (batch-ETA vs deadline → typed
backpressure, never a minutes-long block), and poison isolation. The in-process `StormCoalescer` proved the
ROUTING+COMBO logic; the production version is lane_queue-backed. I10/I13/cross-process are now part of "done".

## REFRAME — GROUNDING + PROGRESS (2026-10-04)
**Grounded `lane_queue` (reuse, don't rebuild):** it is the durable, cross-process spine and ALREADY carries most of
what the reframe needs — rows have `sla_class` ('realtime'|'batch'), `deadline_ts` (absolute SLA), `defer_until`/`parks`
(capacity parking); `lease()` is cross-process-atomic (BEGIN IMMEDIATE) and already schedules REALTIME-before-batch by
TIGHTEST deadline then oldest; `settle()` does class-aware retry; `mark_batched`/`collect_batched`/`requeue_from_batch`
are the batch crash-resume lifecycle; `drain()` leases+runs+settles. So the combo's service-class + deadline model and
the durable/idempotent batch lifecycle are LARGELY PRESENT. The genuinely-missing piece is cross-process RATE
coordination: lane_queue coordinates WORK (who runs what, once), but the rate wall is enforced by dispatch `_Bucket`,
which is IN-PROCESS — so K drainers/processes each pace to 100% of the vendor cap (the panel's #1 finding).

**Fixed (done + guarded this session):** the `_ensure_queue_schema` additive-migration RACE. The old check-then-ALTER
(PRAGMA table_info → conditional ADD COLUMN) is TOCTOU across the concurrent pooled connections: under a fan two
connections both pass the check on a fresh table, the second `ALTER` raises "duplicate column name: sla_class", and
`enqueue` swallowed it to `[]` — the durable row was SILENTLY LOST exactly while a storm was being queued (seen in the
replay). Fixed to the repo's race-safe attempt-and-swallow pattern (bulkgate.py:72). Guard: `tests/test_lane_queue_schema_race.py`
(6 rounds × 24 threads, barrier-forced) — reliably RED on the old pattern, GREEN 5× on the fix (teeth proven).

**Reframe build order:**
- ✅ **(1) cross-process shared RATE budget (panel #1) — DONE + wired + proven.** `src/spendguard/xp_rate_window.py`:
  a sqlite sliding-window reservation (multi-window per-second + per-minute rpm + per-minute tpm, NO burst allowance
  beyond the per-second sub-cap, fail-open on infra not on rate). Wired into `dispatch.acquire` (dispatch.py:~836,
  metered vendors only, before the scarce flock slot; `SPENDGUARD_DISPATCH_XP_RATE_OFF=1` kill-switch). Tests:
  `test_xp_rate_window.py` (deterministic fake-clock math) + `test_xp_rate_window_multiprocess.py` (3 real processes,
  one shared db: aggregate 1s-peak == cap, teeth-verified — limiter OFF → peak 75/0.06s, ON → peak 25/paced). **Real-path
  validation: the incident replay's internal 429s dropped 931 → 0** (the stampede is killed even in ONE process, because
  the shared window has no burst allowance — unlike the in-process `_Bucket`). 0 regressions across 10 dispatch tests.
  ⇒ The 429 SAFETY axis is solved for the metered path: zero surfaced AND zero internal 429s, paced under the wall.
- ✅ **(2) coalesce→combo for LATENCY/deadlock — DONE (engine) + ⭐ GREEN.** `src/spendguard/storm_submit.py`
  `submit_storm(tasks, intent, model, execute_batch, deadline_s, ...)` drives the proven StormCoalescer: realtime rides
  the REAL governed metered path (adapters.call → dispatch.admit → the cross-process rate window → _call_guarded), the
  overflow diverts to the (injected, provider-aware) Batch API, and it COLLECTS N final results demuxed by id in order.
- ✅ **(5) the ⭐ acceptance test is GREEN, 5× deterministic** (`tests/test_incident_replay_storm.py`): control arm has
  teeth (ungoverned burst 429s); guarded arm via submit_storm = 120 paced realtime + 180 batched, all 300 returned,
  canary bijection, **zero surfaced AND zero internal 429s**, inside the 3s horizon. This is the contract the user has
  wanted for a month: storm → pace+batch combo → every result back, no 429.

**STATUS (2026-10-04, commits 9656d04 storm-fix · 66115ea rigor-pass · 5b42ccc 4a):**
- ✅ **(4a) production provider-aware batch executor — DONE.** `storm_submit.default_batch_executor`: provider dispatch
  (openai→submit_chat_tasks+collect_chat_tasks; anthropic→submit_message_batch+collect_message_batch), id-keyed,
  poll-until-ready, no silent drop (whole-submit failure, per-item failure, AND poll-ceiling all return typed results).
  submit_storm defaults to it. Test: `tests/test_default_batch_executor.py`. (Live-batch submit/poll/collect = a
  separate slow+billed validation.)
- ✅ **(I16) deadline→typed backpressure — DONE in submit_storm:** `collect_timeout_s` → a hung/slow batch surfaces as
  `served_via='backpressure'`, never an indefinite block (`tests/test_storm_submit.py`).
- ✅ **(I20) cohort reservation across the horizon — DONE.** The coalescer tracks realtime RESERVED in a rolling
  horizon and a new cohort's realtime share = `budget − reserved`, so a SUSTAINED fan shares one budget-per-horizon
  across cohorts (overflow → batch) instead of each cohort claiming a fresh full budget. Guard:
  `tests/test_storm_coalescer.py` I20 (reservation math + a pre-reserved cohort overflowing entirely to batch).
- ✅ **(4b) both doors — MECHANISM + COMBO proven; flag STILL DEFAULT-OFF pending a live validation.** `storm_route.py`
  routes a labelled `adapters.call` through a shared coalescer keyed by (vendor,model,intent,reasoning,system) at the
  adapters.py:~1058 seam (recursion-guarded by `_route=False`). `tests/test_storm_route_mechanism.py` proves routing
  (flag toggles; N→N; 0 surfaced 429; demux). `tests/test_storm_route_both_doors.py` proves the COMBO through the RAW
  door, 3× deterministic (realtime=budget 40 + batch 60, 0 surfaced 429, demux) — now reliable because of I20. The
  flag (`SPENDGUARD_STORM_COALESCE`) stays DEFAULT-OFF: enabling it process-wide is a rollout decision that wants a
  LIVE both-doors validation first (real provider, real Batch API). Until then the raw door keeps SAFETY from the
  wired xp_rate limiter (0 surfaced 429), and the combo is one env var away.
- 🔜 **(3) durable idempotency / crash-resume (I13/I22) — the last piece.** submit_storm's cohort/futures are
  IN-MEMORY — a SIGKILL mid-batch still loses in-flight work + risks a double-submit. Move the cohort + disposition
  onto `lane_queue` (durable, cross-process) with an idempotency key on batch submit + reattach-on-restart. id-keyed
  demux is done; durable resume is not.

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
