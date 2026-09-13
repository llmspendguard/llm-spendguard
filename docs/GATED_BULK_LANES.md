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

**Tail-hedging (opt-in — off by default).** Dynamic dispatch balances *lanes*; it can't rescue a single *call* that
stalls while its lane is otherwise fine. With `dispatch.lane_hedge_ms` set (config, or env
`SPENDGUARD_DISPATCH_LANE_HEDGE_MS`), a task that hasn't returned a served row within that many ms fires a
**duplicate on the most-free *other* lane** and takes whichever returns first — killing the per-call long tail that
stretches a batch's wall-clock.

- **Always $0.** The hedge runs `no_metered_fallback=True` regardless of the caller's `refuse_billed`, so it can
  only ever cost a free lane miss; the primary keeps the caller's billing semantics.
- **Set it to the intent's measured p90–p95 latency, never lower.** A too-low value hedges *every* task (2× lane
  load for no tail win). `0` (the default) is off — no extra lane load.
- **Diversity stays visible.** A hedge lands on a different vendor and only on the tail; a raced row carries
  `hedged=True` + `hedge_peer=<the lane it raced>`, so any skew toward fast vendors is measurable, never silent.
- **Measure it:** `scripts/probe/dispatch_ab.py` (env `N`, `ROUNDS`, `HEDGE_MS`) reports wall-clock + per-lane
  spread + hedged-count for STATIC vs DYNAMIC vs DYNAMIC+hedge, so before/after is a number, not a guess.

## Why `parsed` exists now

When you pass `schema=`, each row (and `adapters.call`'s result) carries `parsed` — the decoded object, or `None` if
it did not decode — alongside `text`. Scatter `parsed` to your demux; never re-`json.loads` `text` and reinvent a
salvage (an un-decoded body reaching a demux scatters nothing and reads as "the model had little to say", not as a
decode failure). `text` stays for provenance.
