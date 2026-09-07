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

`bulk_delegate`'s built-in miss fallback (`refuse_billed=False`, the default) is a **per-task realtime metered
call** — the most expensive shape, and the one a caller gets without thinking about it. A consumer with a batch path
wants the cheap direction: **lanes first, batch the remainder** (~50% cheaper). The composition is short and the
same for anyone:

```python
# 1) refuse_billed=True → a lane miss is a FREE error row (no text), never a metered realtime call.
rows = lane_balance.bulk_delegate(tasks, intent, schema=SCHEMA, tier="cheap",
                                  checkpoint="run.jsonl", chunk_size=100, refuse_billed=True)

# 2) split — "the lane did not deliver this" is refused / arity-miss / undecodable, all the same to the batch pass.
served   = [t for t, r in zip(tasks, rows) if r.get("text")]
unserved = [t for t, r in zip(tasks, rows) if not r.get("text")]

# 3) batch ONLY the remainder through the gated batch path (§1 gate applies to the batch sig too).
submit_batch(unserved)     # ~50% cheaper than the realtime per-task fallback
```

The point is the **direction** of the degradation: the default fallback is the expensive path, so a caller who has a
batch path should drive the miss set into it explicitly rather than discover the realtime cost on an invoice.

## Why `parsed` exists now

When you pass `schema=`, each row (and `adapters.call`'s result) carries `parsed` — the decoded object, or `None` if
it did not decode — alongside `text`. Scatter `parsed` to your demux; never re-`json.loads` `text` and reinvent a
salvage (an un-decoded body reaching a demux scatters nothing and reads as "the model had little to say", not as a
decode failure). `text` stays for provenance.
