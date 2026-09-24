# Conformance & Soak Suite — live, budgeted, repeatable

**Status: SPEC (design phase). No live spend until the zero-spend estimate is reviewed and approved.**

## 1. Why this exists

The 363-test offline suite mocks providers, so it proves LOGIC but never that the real rails hold under real load.
Every expensive incident this project has had was a *live* behaviour the offline suite could not see:

- a 532-task fan raised `DispatchTimeout` and **crashed** honestreview; a confined fan returned **766 empty rows**.
- a bc_edges fan **429-stormed** Opus at 8 workers.
- a gpt-5.5 fan cost **$51.88 vs a $13.69 estimate** (9×) and **495 calls were cut mid-reasoning**, billing reasoning
  tokens that produced nothing — **invisible** to the local ledger.
- `metered_only=True` still got **nano → opus** via a $0 lane.

This suite drives REAL providers + lanes to prove those behaviours are closed, and asserts on the **observability we
now expose** (`dispatch.admission_state`, `queue_depth.parked`, `learned_limits`, `deadline_cancels`, result rows) — so
each check is a hard pass/fail, not "looked fine." It is a live conformance test AND a soak/stress test in one.

Cross-reference `docs/INCIDENTS.md` (the incident log) — every behaviour below cites the incident it guards.

## 2. Principles (non-negotiable, from the repo doctrine)

1. **Estimate-first, approval-gated.** Every run does a SEPARATE zero-spend estimate (token count × `pricing.py`) →
   per-behaviour $ projection → explicit approval → run. Built on `bulkgate.gated_batch` (estimate→test→eval→run).
2. **Staged.** Behaviours scale 100 → 250 → 500 → 1000; a stage that misbehaves STOPS the ladder (no scale-on-red).
3. **Hard budget stop.** A running $ tally (real API, split from est-value) hard-stops at the approved ceiling; a
   completed call still bills, so we never cancel-as-cost-control — we STOP SUBMITTING.
4. **Under the gate.** The suite runs gated (`spendguard.require()`); it PROVES the rails by using them.
5. **Observable assertions only.** Each behaviour asserts a fact read from a surface, never a vibe. Deterministic where
   possible; where stochastic (real latency/429s), assert an INVARIANT ("0 unhandled 429 reached the caller"), not a
   number.
6. **Repeatable.** One `spendguard conformance` command, a versioned behaviour manifest, recorded outcomes, re-runnable
   with estimate-first every time.

## 3. Behaviour matrix

Each behaviour = an incident → an induced live scenario → an observable assertion → a spend class. `$0` = subscription
lanes only; `metered` = requires the paid API (the soak allocates most of the budget here per the chosen "fuller
metered soak").

| # | Behaviour | Incident | How it's induced | Assertion (surface) | Spend |
|---|---|---|---|---|---|
| B1 | Fan never CRASHES | honestreview DispatchTimeout | 500-task fan on a deliberately-saturated lane (low `lane_concurrency`) | 0 exceptions; `len(results)==N`; every row has text or a typed reason | $0 |
| B2 | Fan never EMPTY | honestreview 766 empties | confined `lanes=` all cooling | run widens (loud) or honest `no_viable_lane`; 0 silent "" | $0 |
| B3 | No 429 under load | bc_edges Opus @8 | heavy metered fan at/above a set `tpm_<vendor>` | 0 unhandled 429 to the caller; `admission_state.governor` shows waiting>0; provider 429s (if any) were absorbed/paced | metered |
| B4 | Self-calibrate from 429 | Step 3 | force an unknown low `tpm`, drive a burst | after the first 429, `learned_limits[vendor].tpm` is set; subsequent burst 0-429 | metered |
| B5 | Lane → metered failover | lane cooldowns | cool the lane mid-fan (`_lane_cool`) | result `executor` flips lane→`api-fallback`; still served | $0→metered |
| B6 | Metered → BASE-model failover (tier-3) | tier-3 ladder | pin a bad served-model id so the chosen model errors, `base_fallback=True` | result `model` == provider base; `substituted_from` set | metered |
| B7 | Reasoning NOT cut mid-thought | the $51 | reasoning fan (gpt-5.x) with the reasoning deadline floor on | `deadline_cancels` ≈ 0; all tasks return a verdict | metered |
| B8 | Reasoning-cut DETECTED | the $51 | same fan, deadline forced below reasoning latency | `note_deadline_cancel` counter increments; loud line emitted | metered |
| B9 | Estimate ACCURACY | $33/$175, $13.69/$51.88 | estimate-first a reasoning sample, then run it | actual within ±X% of the reasoning-inclusive estimate (X TBD, target ≤25%) | metered |
| B10 | metered_only PINS the model | warden nano→opus | `bulk_delegate(metered_only, model_for=pin)` panel | `panel_providers` == requested set; 0 substitutions | metered |
| B11 | max_tokens no silent truncation | bc_edges | structured schema call, tiny caller `max_tokens` | reply floored (not empty-as-no-findings); `_warn_once_caller_maxtokens` fired | metered |
| B12 | PARKING under saturation | Step 4 | saturate the queue, `submit`/`drain` | `queue_depth.parked` > 0 during, drains to done; parks didn't burn `attempts` | $0 |
| B13 | Double-record suppressed | route_through_queue | `drain` a fan of N | exactly N `done` rows (no 2N); `record_route=False` held | $0 |
| B14 | metered_only ⇒ no lane substitution | warden bandit collapse | metered_only fan, bandit denylist unset | served model == pinned; no cross-vendor collapse (`panel_providers`) | metered |

The build's FIRST step (§6) is a lane-routed comprehension pass over the other transcripts to CONFIRM this matrix is
complete and add any missed class before we lock it.

## 4. Corpus (real + synthetic)

**Real (fidelity):** mine actual tasks from the repos for realistic token + reasoning distributions —
- bc_edges evidence-family classification (lmm) — reasoning + packed envelopes,
- goedel-warden provenance cross-check — metered_only + schema,
- honestreview file reviews — big-context cross-vendor panels,
- LOINC/vocab typing (lmm) — bulk short-output classification.
Extracted via lane-routed comprehension ($0), de-identified, frozen into `scripts/integration/conformance/corpus/*.jsonl`.

**Synthetic (extremes, deterministic):** parametrized generators for the edges the real corpus won't reliably hit —
huge context (near the input window), deep-reasoning triggers, packed envelopes that drop ids, forced-failover fixtures
(bad model ids, cooled lanes), and a saturation load generator. Seeded → reproducible.

Total available: thousands of items. The LIVE-spend subset per behaviour is a bounded, estimate-first SAMPLE; the rest
run on $0 lanes or as replay/assertion-only.

## 5. Budget model — "fuller metered soak" within $50

- Zero-spend estimate produces a per-behaviour projection; the soak weights the metered behaviours (B3–B11, B14) to
  use the bulk of the $50 for genuine 429/pacing/failover realism, with $0-lane behaviours (B1, B2, B12, B13) free.
- Draft allocation (refined by the real estimate, hard-stop enforced):
  - B3/B4 no-429 + self-calibrate — the largest metered slice (needs real burst load): ~$18
  - B7/B8/B9 reasoning economics (reasoning tokens are the pricey axis): ~$15
  - B5/B6 failover chains: ~$6
  - B10/B11/B14 metered_only + max_tokens + no-sub: ~$6
  - headroom/retries: ~$5
- **Batch API** for non-interactive volume (½ cost). **Hard-stop** at the approved ceiling. Real $ shown split:
  `$X API :: est sub value $Y (lane-served)`, never summed.

## 6. Harness architecture

- `scripts/integration/conformance/` — the gated driver + generators + corpus + the behaviour manifest
  (`behaviours.py`: one entry per B# with `setup`, `run`, `assert`, `spend_class`, `stages`).
- `spendguard conformance --estimate` → zero-spend projection (per behaviour + total, vs the ceiling).
- `spendguard conformance --run --budget 50` → staged execution, live tally, hard-stop, records each outcome to
  `conformance_runs/<ts>.jsonl` (behaviour, stage, pass/fail, evidence, $).
- `spendguard conformance --report <run>` → the pass/fail matrix + $ + the observability snapshots captured.
- Re-runnable; estimate-first every time; results are durable evidence, not console scrollback.

## 7. Execution plan

1. **Mining pass** (lane-routed, $0): complete the behaviour matrix + extract the real corpus.
2. **Build** the harness, generators, manifest, and the `spendguard conformance` command ($0; offline-testable parts
   get their own hermetic guards in the main suite).
3. **Zero-spend estimate** → bring the per-behaviour $ breakdown for **approval**.
4. **Staged live run** (100→250→500→1000), hard-stop at $50, record everything.
5. **Report** — the conformance matrix + real $ (split) + the captured observability.

## 8. Open decisions (to settle before the live run, not before the build)

- B9 accuracy bar `X%` (proposal: ≤25% for a reasoning class after seeding; tighter once measured).
- Which vendors/models to soak (proposal: one OpenAI reasoning model + one Anthropic + one $0 lane, to exercise both
  SDK header paths and the lane→metered→base chain).
- Whether the metered soak runs against a low deliberately-set `tpm_<vendor>` (safer, provokes pacing at low $) vs the
  real provider limit (more realistic, higher $).
