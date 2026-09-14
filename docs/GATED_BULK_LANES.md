# Bulk on the lanes: gating it, and degrading it to Batch

Two recipes a consumer needs once its bulk work rides `bulk_delegate` end to end: how to run a lane fan through the
estimate→test→eval gate, and how to degrade a lane miss to the Batch API (the cheap direction) instead of the
per-task realtime fallback (the convenient, expensive one).

## 1. A GATED lane fan — the `gated_batch` × `bulk_delegate(gate_sig=)` seam

A lane fan is ONE job (one intent) spread across several lane models. The gate is about that WORK class, not any one
model — and `bulkgate.gate_status` looks a sig up by **sig alone** (`WHERE sig=?`), so **one sig covers the whole
fan**; the `model` argument to `gated_batch` is only the estimate's label. Open the gate with that one sig, run
estimate→test→eval, then hand the **same** sig to `bulk_delegate(gate_sig=…)` — whose own bulk gate then reads
`gate_status(sig).fresh` and lets the fan run instead of printing `WOULD-BLOCK`.

```python
from spendguard import bulkgate, lane_balance

intent = "warden:describe-card"
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["id", "label"],
    "properties": {"id": {"type": "string"}, "label": {"type": "string"}}}}}}

# ONE sig for the fan. template_id=intent is the WORK class; the "lane-fan" label stands in for a model because the
# gate keys on the sig, not the model. (Do NOT open one gated_batch per arm — the fan is one job.)
sig = bulkgate.sig("lane-fan", template_id=intent)

with bulkgate.gated_batch(sig, "lane-fan") as job:
    job.note_estimate(worst_case_usd, len(tasks))
    # TEST runs a SMALL REAL fan on the SAME lanes+schema path, so the shape is proven where it will actually run.
    # force=True skips bulk_delegate's OWN bulk gate on the sample itself (the sample IS the test).
    job.test(n=8,
             run_fn=lambda: lane_balance.bulk_delegate(tasks[:8], intent, schema=SCHEMA, tier="cheap", force=True),
             contract=SCHEMA, items=tasks[:8])
    job.eval(bar="a valid {results:[{id,label}]} envelope, one object per card id")

# sig is now fresh (estimate + test + eval). Pass the SAME sig — bulk_delegate's bulk gate reads gate_status(sig).fresh.
rows = lane_balance.bulk_delegate(tasks, intent, schema=SCHEMA, tier="cheap",
                                  checkpoint="run.jsonl", chunk_size=100, gate_sig=sig)
```

Answers to the four seam questions:
- **Same sig?** Yes — open `gated_batch(sig, …)` and pass `gate_sig=sig`. `gate_status` keys on the sig; passing a
  different value is the double-key failure the `sig=` fix warns about.
- **One model for a multi-model fan?** The sig is model-agnostic for the gate; build ONE sig for the fan (label the
  `model` arg "lane-fan" or the tier). One `gated_batch` per fan, not per arm.
- **`gated_submit` vs `gate_sig=`?** Prefer `gate_sig=` (one gate, the line above). `job.gated_submit(count, est,
  lambda: bulk_delegate(..., force=True))` also works, but then `bulk_delegate` must `force=True` or it double-gates.
- **`job.test` run_fn — a fan or a single call?** A SMALL REAL FAN (`bulk_delegate(tasks[:n], …, force=True)`), so the
  test exercises the lanes+schema path the real run will take.

## 2. Degrade a lane miss to BATCH, not per-task realtime

`bulk_delegate`'s default miss fallback (`on_miss="api"`, the old `refuse_billed=False`) is a **per-task realtime
metered call** — the most expensive shape, and the one a caller gets without thinking about it. A consumer with a
batch path wants the cheap direction: **lanes first, batch the remainder** (~50% cheaper). Pass `on_miss="batch"`
with a `batch_submit` and it is built in — one call:

```python
# on_miss="batch": run lanes with NO realtime fallback, then submit the WHOLE miss set ONCE through your gated
# batch path. Async by construction — bulk_delegate never blocks on the 24h window; the missed rows come back
# reason="queued_batch" carrying the handle your submit returned. return_keyed re-associates them by MEANING, so a
# caller that deduped/reordered can never cross a queued row onto the wrong task.
rows = lane_balance.bulk_delegate(
    tasks, intent, schema=SCHEMA, tier="cheap", checkpoint="run.jsonl", chunk_size=100,
    on_miss="batch", batch_submit=lambda misses: submit_batch(misses),   # returns a batch id/handle
    task_key=lambda t: t["id"], return_keyed=True)                       # {id: row}

queued = {k: r["batch"] for k, r in rows.items() if r.get("reason") == "queued_batch"}   # poll these later
served = {k: r for k, r in rows.items() if r.get("text")}
```

Omit `batch_submit` and the misses come back `reason="batch_eligible"` instead — a structured signal to route them
into your own batch path (the split, done for you). A **deliberate** stop from `batch_submit` (a spend refusal)
HALTS the fan; any other submit failure leaves the misses `batch_eligible` with a loud notice, never a false
"queued". Before fanning, `estimate_fan(tasks, intent, tier="cheap")` previews the whole thing for **$0** —
distinct-call count, the lanes it would use, and the worst-case metered $ ceiling if every task fell to the API —
so the batch-vs-lane decision is made against a number, not discovered on an invoice.

## 3. Dispatch tuning — dynamic least-loaded (default) + optional tail-hedging

The fan spreads tasks across lanes in two independent ways; the first is on always, the second is opt-in.

**Dynamic least-loaded (the default, nothing to set).** Each task is assigned to the lane with the **most free
dispatch capacity right now** (`dispatch.lane_free = limit − in_flight − waiting`), not by a static `arms[i % n]`
round-robin. Static binding head-of-line-blocked: a task bound to a momentarily-slow lane queued its whole share
while the fast lanes finished and idled, so the wall-clock became the *slowest* lane's chain and the speedup swung
run-to-run. Least-loaded still **preserves the cross-vendor spread** — an empty lane is fully free, so every vendor
gets seeded first; only then do fast lanes pull the overflow (it never collapses the panel to one lane). Set
`SPENDGUARD_LANE_STATIC_DISPATCH=1` to revert to round-robin (A/B or a safety fallback).

**Measured, and the win GROWS with scale** (dispatch_ab.py, 3 live lanes, bandit pinned off): at N=96 static median
**47.9s → dynamic 28.7s (~1.67×)**, dynamic correctly shifting load off the slow lane (claude-code 32→14) onto the
fast ones. (At small N — 16 — the lanes aren't saturated, so it's a wash, 8.9s → 8.5s: dispatch policy matters most
exactly when the fan is big enough to saturate, which is when it matters.)

**Tail-hedging (opt-in — off by default).** Dynamic dispatch balances *lanes*; it can't rescue a single *call* that
stalls while its lane is otherwise fine. With `dispatch.lane_hedge_ms` set (config, env
`SPENDGUARD_DISPATCH_LANE_HEDGE_MS`, or per-call `bulk_delegate(hedge_ms=…)`), a task that hasn't returned a served
row within that many ms fires a **duplicate on the most-free *other* lane** and takes whichever returns first —
killing the per-call long tail that stretches a batch's wall-clock.

- **The SPARE-CAPACITY GATE makes it safe on any fan size — this is the key property.** A hedge fires only when the
  chosen other lane has a genuinely free slot right now (`dispatch.lane_free(hlane) > 0`). So on a saturated bulk fan
  (N ≫ total concurrency) every lane is full → **no hedge fires**, and the duplicate can never pile onto busy slots.
  MEASURED, same catastrophic config before/after the gate (dispatch_ab.py, N=96, `hedge_ms=3000`): **92–94 of 96
  hedged → 0–8**, and the wall went from **65.7s (2.3× slower than dynamic) back to ≈ dynamic**. The gate self-limits
  hedging to small fans with idle capacity, so the same setting is safe for a small per-edit fan and a wide repo wave.
- **Always $0.** The hedge runs `no_metered_fallback=True` regardless of the caller's `refuse_billed`, so it can
  only ever cost a free lane miss; the primary keeps the caller's billing semantics.
- **Set it near the intent's measured p90–p95** so only the slowest tail (the calls dominating the wall) is raced;
  the gate makes over-hedging harmless, but a value near the median wastes $0 plan calls hedging non-stragglers.
  MEASURED here (this account, ~20k honestreview calls/mo): per-call p50 **4.5s** · p90 **11.4s** · p95 **15.7s** ·
  p99 **33.5s** · max **366s** — a real long tail. `dispatch.lane_hedge_ms=12000` (≈ p90–p95) is enabled by default
  on this deployment: it races honestreview's slowest ~10% (the tail that dominates a per-edit fan) onto a free lane
  and leaves the other 90% untouched. (Shipped code default is `0` = off, since a hedge spends an extra plan call;
  enable per deployment with `spendguard config set dispatch.lane_hedge_ms <ms>`.)
- **Diversity stays visible.** A hedge lands on a different vendor and only on the tail; a raced row carries
  `hedged=True` + `hedge_peer=<the lane it raced>`, so any skew toward fast vendors is measurable, never silent.
- **Measure it:** `scripts/probe/dispatch_ab.py` (env `N`, `ROUNDS`, `HEDGE_MS`) reports wall-clock + per-lane
  spread + hedged-count for STATIC vs DYNAMIC vs DYNAMIC+hedge, so before/after is a number, not a guess.

## 4. Pinned-vendor matrix — the provider-locked atomic pair

For a cross-vendor CONSENSUS panel (N files × M *named* vendors — e.g. honestreview's repo review, where WHICH
vendor answered is the measurement), pass `model_for` (a callable `task → "vendor:model"`) instead of a tier. Each
task is **pinned** to its exact vendor and never bandit-substituted. Submit the whole matrix, set no concurrency
number; the governor bounds per-vendor in-flight and queues the rest, and `return_keyed` gives back `{key: row}`.

```python
rows = lane_balance.bulk_delegate(
    tasks, "review:panel", model_for=lambda t: t["vendor_model"],   # e.g. "openai:gpt-5.6-sol", "anthropic:claude-opus-4-8"
    task_key=lambda t: t["id"], return_keyed=True, checkpoint="panel.jsonl", chunk_size=100)
```

**A pin is a PROVIDER + a reasoning FLOOR, realized as an atomic (lane, metered) pair.** Each task rides its $0
subscription **lane first** (an `openai:` pin → the codex lane, `anthropic:` → claude-code, `gemini:` → the agy/Gemini
subscription lane), and a lane miss falls back to **that same provider's metered API** — never a different vendor.
The fallback reasoning is **EQUAL-OR-GREATER**, resolved by the canonical map (`reasoning_equivalence.resolve_metered`):
equal → a bake-off-proven-equal *lesser* → else round *up*; it never under-reasons. So the pair is faithful for
capability ("vendor X judged this at ≥ the requested reasoning") while staying $0 when the plan can serve it.

**Force the metered half when reproducibility demands it — `metered_only=True`.** The atomic pair is faithful for
*capability*, but a *verdict-cached* fan (a refuter that caches and compares verdicts) needs the stricter property
that a parallel fan reproduces the serial verdict *distribution*. There, the $0 lane's warm-daemon concurrency can
drift a borderline verdict even at equal reasoning. `bulk_delegate(..., metered_only=True)` (→ `adapters.call(metered_only=True)`)
runs every pinned vote on the concurrency-invariant **metered API**, skipping the lane. It BILLS — the deliberate
trade for a fan where the verdict distribution is the product. Default is the $0 atomic pair; this is the opt-in for
the narrow reproducibility case (e.g. honestreview's refute fans).

Each row shows exactly **how that vote ran**, so a consistency-sensitive caller can verify it:
- `served_by_metered_api` — `True` if the paid API served it, `False` if the $0 lane did (which half of the pair).
- `model` — the vendor:model that actually answered (a within-vendor served-id resolution, e.g. a dated Anthropic
  id, is still the same vendor).
- `reasoning` (requested) and `effort` (applied) — the tier.

The VISION case (`images_for`) rides the metered API by construction — the lanes are text-only CLIs.

### The reasoning-equivalence map (the one place the pair is proven)

`src/spendguard/reasoning_equivalence.py` is the single, derived, persisted source of truth for how every
`(lane, model, reasoning)` maps to its same-provider metered call. It unifies what used to be scattered across
`models.normalize_reasoning`, `codex_exec._codex_effort`, `lane_catalog.REASONING_QUIRK` and
`adapters.metered_fallback_id`, and it **verifies the equal-model metered call is actually priced + served** (an
`availability` of `yes` / `unverified` / `no` per cell), so a stale alias can't silently strand a fallback. Inspect it:

```
spendguard lanes --reasoning-map      # every lane × model × level → the same-provider metered call (equal model,
                                      #   equal-or-greater reasoning), availability ✓/?/✗, and status
spendguard lanes --fallback           # the lane→metered id equivalence (a down plan degrades, never strands)
```

- **Provider-locked.** `metered_fallback_id` only re-spells the id *within* the same vendor (and resolves a stale
  bare alias to its served dated id, e.g. `claude-haiku-4-5` → `claude-haiku-4-5-20251001`, $0 from the served-list
  cache). A pinned agy/Gemini call can never fall back to codex.
- **`proven_lesser` needs an AGENTIC verdict.** Recording that a cheaper reasoning tier is "equally good" is a
  MEANING judgement, so `record_equivalence(...)` accepts it ONLY with an affirmative LLM-judge verdict
  (`{judged_equal: True, judge_model, sample_n>0, …}` from a bake-off) and refuses free-text. Learnings persist and
  overlay the derived map.

## Why `parsed` exists now

When you pass `schema=`, each row (and `adapters.call`'s result) carries `parsed` — the decoded object, or `None` if
it did not decode — alongside `text`. Scatter `parsed` to your demux; never re-`json.loads` `text` and reinvent a
salvage (an un-decoded body reaching a demux scatters nothing and reads as "the model had little to say", not as a
decode failure). `text` stays for provenance.
