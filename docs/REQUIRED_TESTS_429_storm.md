# REQUIRED TESTS — "always success, always returned, zero surfaced 429s, for 1 or 1000s, all providers"

The complete test set that must pass before the storm→batch requirement is **solved** — consolidated from a 5-model
cross-vendor panel (opus-5 / gpt-5.6-sol / gemini-3-pro / kimi / glm-5.3, 2026-10-04). The five converged hard; this
is the deduped union. **This is the seed for the `required_tests` manifest (honestreview Prompt 3).** Status today:
**essentially all RED** — the decision functions exist but the end-to-end behavior is unwired, so almost none of this
passes. Nothing below may be grandfathered into a weaker assertion.

## §0 — The evidence harness (the un-fakeable substrate every other test stands on)
Build these FIRST; without them the rest are fakeable.
- **E0.1 caller-side 429 collector** — a wrapper around the ONE entry point records every response/error the *caller*
  sees; `surfaced_429_count` is measured HERE, never from spendguard's own logs. Any test claiming "zero 429" fails on `count>0`.
- **E0.2 egress proxy / provider simulator that can FAIL** — all provider HTTP passes through an instrumented proxy
  that (a) counts every call, (b) distinguishes `/realtime` vs `/batch` endpoints, (c) enforces per-vendor multi-window
  caps and throws a **real 429** on burst. No provider client may be constructed without it (import-lint + runtime check).
- **E0.3 control-arm calibration (the single most important anti-fake device)** — every live/sim "zero-429" test first
  runs the identical workload WITHOUT spendguard; the control MUST produce ≥1 429 (burst tests: ~the incident rate). If
  the control doesn't bleed, the test is marked **VACUOUS — INVALID**.
- **E0.4 canary substrate** — each request embeds a unique token `T_i`; assertions available everywhere: response for
  future *i* contains `T_i`, and multiset(returned canaries) == multiset(submitted) — a bijection (catches
  swapped/dropped/duplicated/mis-unbundled results).
- **E0.5 provider source-of-truth** — "billed>0" and "no double-spend" are verified against the **provider's own**
  usage/batches API (`openai.batches.list()`, Anthropic console/usage), never the local ledger alone.

## A — End-to-end N→N conservation (the core contract)
- **A1 empty** N=0 → `[]`, zero provider/lane/batch/ledger calls.
- **A2 N=1 realtime per provider** — 1 correct nonce-bound response; one admission; no batch; <deadline.
- **A3 count exactly equals** for N∈{1,2,999,1000,1001,5000}: `out==in`, no off-by-one at chunk boundaries.
- **A4 original order preserved** after auto-batch+unbundle across multiple jobs (nonce order == request order).
- **A5 content correctly demuxed** — response[i] answers request[i] (canary), proves unbundling maps to the RIGHT request.
- **A6 no dropped request under partial batch failure** — batch returns 1998/2000, the 2 are retried, final count==2000.
- **A7 thousands realtime-paced when batch forbidden** — N≥2000, policy forbids batch, generous deadline: paced, rolling rpm/tpm never exceeds cap, N correct, no raw/surfaced 429.

## B — Auto-batch actually wired + correct (the behavior, not the decision function)
- **B1 autobatch triggered on storm** — burst exceeds rpm → ≥1 real batch job created+run (batch_job_ids non-empty via proxy/provider), realtime did NOT emit the storming volume.
- **B2 NOT triggered for small burst** — below threshold → 0 batch jobs, served realtime/lane (prevents "always batch" cheating).
- **B3 decision→execution wiring proven** — when `should_batch_fan()`/`chunk_for_batch()` fire during a storm, the batch executor's create+poll+fetch+unbundle are each invoked and ≥1 job results (fails today by construction).
- **B4 results returned through the same future** — the caller's N awaitables resolve from batch output; provenance per response says `served_via=batch`, none silently fell back to realtime.
- **B5 chunk respects item/byte/token caps** — each job ≤ provider caps (actual envelope bytes, not token heuristics), union==N, no overlap.

## C — One chokepoint (nothing bypasses the admission door)
- **C1 all outbound calls pass through admit** — transport-boundary counter: `admit_passes == provider_egress_calls` (equality at the socket/httpx boundary, not ≥ at the app boundary). Every egress carries an admit token.
- **C2 retries re-enter admit** · **C3 unbundler-resubmit re-enters admit** · **C4 batch-poller is itself governed** (polling can't storm).
- **C5 static SDK-bypass scan** — AST/import lint: no module outside the admission layer calls a provider SDK `.create()`; zero violations (CI gate).
- **C6 no second door** — enumerate every public entry (realtime/lane/metered/bulk/queue-drain/batch/retry/stream/embed); each increments the SAME shared admit counter; a runtime egress guard rejects un-admitted traffic.

## D — Per-provider + cross-provider
- **D1 per-vendor golden storm** — each of anthropic/openai/gemini/zai/moonshot with its own caps: N→N, 0 surfaced 429, window rate ≤ cap, batch limits respected.
- **D2 cold-cap seed active from call #1, ALL vendors** — first-ever call to each is already paced (no warm-up storm).
- **D3 cross-provider simultaneous storm, no starvation** — one mixed submission; per-vendor pacing independent; a congested vendor doesn't block an idle one; global count exact; per-provider 429==0.
- **D4 provider without batch API degrades safely** — controlled pacing/backpressure, still 0 surfaced 429, no crash.

## E — metered_only truly rides the metered API (the lmm complaint)
- **E1 billed>0 from provider-reported usage** — metered_only fan → ledger `billed>0` AND a `provider_billing_id`/batch id that exists in the **provider's** API; NOT served by any $0 lane/cache. (Un-fakeable via E0.5.)
- **E2 reconciles with provider invoice** — Σ(ledger billed) == provider usage total within tolerance for the window.

## F — Concurrency / cross-process / sustained
- **F1 cross-PROCESS burst shared cap** — K separate processes fire simultaneously at one spendguard (shared store); aggregate rate ≤ vendor cap; total surfaced 429==0; total out==K·M. (In-process state is NOT enough.)
- **F2 incident replay 4,584 vs 1,000** — sustained incident shape; outbound realtime ≤ 1000 rpm (overflow auto-batched); surfaced 429==0 (vs 8,088 historical).
- **F3 boiled-frog sub-window** — hold just under the cap across many windows + a per-second cap; max dispatch in any 1s window ≤ per-sec cap; no late-onset 429 burst.
- **F4 TOCTOU / no double-admit** — 1000 threads call admit in 50ms; realtime admissions (measured from ledger inter-arrival, not in-process counters) ≤ cap; overflow diverts; 0 429.
- **F5 coalesce-window correctness** — a burst within ~1s forms ONE admission cohort / ONE batch plan (`decide()` O(1), not per-request); N=1 is not needlessly delayed.

## G — Idempotency / resume (no double-spend, no dropped request)
- **G1 SIGKILL mid-batch → all N returned** — `kill -9` after jobs submitted, before fetch; restart; all N eventually returned, 0 dropped (graceful shutdown proves nothing — must be SIGKILL + transactional store).
- **G2 no double-spend after crash** — each request billed ≤ once (idempotency key), reconciled against provider usage (live variant).
- **G3 in-flight batch reattached, not recreated** — restart reattaches/polls existing batch ids; zero duplicate jobs.

## H — Deadline → typed backpressure, never a raw 429
- **H1 deadline met → realtime** (not needlessly batched).
- **H2 unmeetable deadline → typed `BackpressureError`/retry_after**, status≠429, structured, documented.
- **H3 no raw 429 EVER surfaces** — inject a provider 429 on realtime, lane, metered, batch-submit, AND batch-poll; in no case does a raw 429 reach the caller — each absorbed (retry/batch) or converted to typed backpressure.

## I — Batch API's own limits governed (don't move the storm to batch)
- **I1 batch-submit rate governed** (0 batch 429) · **I2 item/byte/token caps per job** · **I3 max concurrent batches honored** (excess queued).

## J — Economics (still optimal)
- **J1 prefers a $0 lane when it satisfies** (ledger billed==0 for those) · **J2 prefers batch pricing when the deadline allows** (unit cost == batch tier) · **J3 no overpay** (1000-req bulk ≤ budgeted batch cost + tolerance).

## K — Ledger evidence (makes "done" measurable)
- **K1 one row per request** (request_id, served_via, provider, status, billed, timestamps) · **K2 surfaced_429==0** (internal_429 may be >0 — distinguishes absorbed vs surfaced) · **K3 reconciles with provider usage** · **K4 incident-replay provenance split sane** (batch_served>0, realtime_served ≤ cap bound).

## ⭐ THE single most important test (unanimous across all 5 models)
**`incident_replay_live_zero_surfaced_429_with_control`** — replay the real 4,584 req/min incident against a 1,000-rpm wall, full topology (multi-node, real Redis/Postgres), live provider:
- **Control arm (spendguard OFF):** surfaced 429 > 0 (ideally ~thousands, matching the 8,088) — proves the harness has teeth.
- **Guarded arm (spendguard ON), all simultaneously true:** (1) N-in == N-out `200`s with valid completions; (2) canary checksum request#i == response#i; (3) egress proxy shows ~realtime ≤ cap + the rest to the batch endpoint (auto-batch taken + unbundled); (4) surfaced 429 == 0; (5) billed > 0 reconciled with the **provider's** usage API.
Green here, with live provider evidence and a 429-producing control ⇒ **solved.**

## The 3 most-faked tests → how they're made un-fakeable
1. **"auto-batch prevents 429s"** — faked by mocking httpx to accept infinite 200s. → Un-fake: E0.2 proxy throws a **real** 429 on burst; the test passes only if traffic hit `/batch`.
2. **"metered_only bills"** — faked by writing a cost number into the local ledger. → Un-fake: E0.5 reconcile against the **provider's** usage/batches API.
3. **"resume/idempotency"** — faked by graceful shutdown. → Un-fake: `kill -9` at a random ms during batch submit; prove state is in a transactional store, not RAM.

## Current existing tests are insufficient (per the panel)
- `tests/test_dispatch_no_429_storm.py` — asserts governor primitives + decision-function return values; does NOT submit N and get N back, nor prove auto-batch is taken/unbundled. Keep its guards, but it does not prove the contract.
- `test_bulk_batch_autosubmit.py` (panel-cited) stubs execution + returns `queued_batch`, not final responses.
These become inputs to the manifest as PARTIAL coverage; the A–K + §0 tests above are the ones that prove "solved."
