"""Backfill the cost (+ where available, quality) corpus from your REAL history — no spend.

Reads the OpenAI + Anthropic batch ledgers (free GETs / the local Anthropic cache), writes one
`calls` row per billed batch (model, cost, tokens, date) and seeds a `run` node in the learning
graph. Optional intent_map (batch_id -> intent) tags rows so the advisor can reason per-intent.
Quality is mostly null here (we didn't record it live) — Layer 2 reconstructs it by mining the
post-event conversation + script evolution. This solves cold start and enables the backtest.

Exposed as: spendguard.backfill() (fn) · `spendguard backfill` (CLI) · a Claude skill (see SKILL.md).
"""
import os, json
from . import calls, learn, pricing


def _openai_rows():
    from .reconcile_openai import load_key, fetch_batches, day
    out = []
    for b in fetch_batches(load_key()):
        if b["status"] not in ("completed", "cancelled"):
            continue
        u = b.get("usage") or {}
        it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not it and not ot:
            continue
        cost = pricing.batch_cost(b["model"], it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
        out.append(("openai", pricing.normalize(b["model"]), cost, it, ot, day(b), b["id"]))
    return out


def _anthropic_rows():
    from . import reconcile_anthropic as ra
    ra.cost_by_day()  # ensure the local usage cache is fresh
    cache = {}
    if os.path.exists(ra.CACHE_PATH):
        with open(ra.CACHE_PATH) as _fh:              # closed deterministically
            cache = json.load(_fh)
    out = []
    for bid, rec in cache.items():
        for mdl, mm in rec.get("by_model", {}).items():
            # AN UNPRICEABLE MODEL IS UNKNOWN, NOT FREE — and not absent either. cost=0.0 was appended as
            # though it were a measurement, so a model missing from the price table backfilled real
            # historical spend into the ledger at ZERO: the exact failure record_unpriced() exists to
            # prevent everywhere else, and what set_price's own docstring warns about ("a $0 rate records
            # real spend as free, silently").
            #
            # Dropping the row instead would be a different loss — the batch really happened, and a
            # backfill that quietly omits it under-reports history. So the row is KEPT with cost=None,
            # which every consumer here already reads as "not priced", and the model is named so it can be.
            try:
                cost = pricing.batch_cost(mdl, mm.get("in", 0), mm.get("out", 0))
            except Exception as e:
                cost = None
                import sys as _sys
                _sys.stderr.write(f"[backfill] UNPRICED {mdl} in batch {bid} ({type(e).__name__}: "
                                  f"{str(e)[:60]}) — the row is kept with cost=UNKNOWN, not $0. Price it: "
                                  f"`spendguard price {mdl} --in <$/1M> --out <$/1M> --source '<url>'`\n")
            out.append(("anthropic", mdl, cost, mm.get("in", 0), mm.get("out", 0), rec.get("created_at"), bid))
    return out


def backfill(intent_map=None, providers=("openai", "anthropic")):
    """Ingest the real batch ledgers into `calls` + the learning graph. Returns (rows, dollars)."""
    intent_map = intent_map or {}
    rows = []
    if "openai" in providers:
        rows += _openai_rows()
    if "anthropic" in providers:
        rows += _anthropic_rows()
    with learn._lock:  # idempotent: skip batches already ingested (run-node id == batch id)
        have = {r[0] for r in learn._db().execute("SELECT id FROM graph_nodes WHERE type='run'").fetchall()}
    total = 0.0
    added = 0
    for provider, model, cost, it, ot, ts, bid in rows:
        # `have` IS UPDATED AS WE GO. It was loaded once from the graph and never added to, while the row
        # stream emits ONE ROW PER MODEL within a batch — all sharing that batch's id. So a batch with
        # three models inserted three run nodes under one id and counted its cost three times, and only a
        # LATER run of backfill would notice, by which point the graph already held the duplicates.
        if bid in have:
            continue
        have.add(bid)
        intent = intent_map.get(bid)
        cid = calls.insert(provider, model, "batch", cost, in_tok=it, out_tok=ot,
                           ts=ts, intent=intent, who="backfill:ledger")
        learn.add_node("run", f"{provider}:{model}",
                       attrs={"cost": (round(cost, 4) if cost is not None else None),   # unpriced stays None, never round(None)
                              "intent": intent, "date": ts, "call": cid, "batch": bid},
                       ts=ts, id=bid)
        total += (cost or 0.0)          # an UNKNOWN cost adds nothing to the total and is not called zero
        added += 1
    return added, total


def load_intent_map(path):
    """Load a {batch_id: intent} JSON, or a dir of files whose stem is the intent and which contain
    an 'id' / 'ids' field (e.g. a pipeline's data/batches/*_batch_id.json)."""
    m = {}
    if os.path.isdir(path):
        for fn in os.listdir(path):
            if not fn.endswith(".json"):
                continue
            intent = fn.replace("_batch_id", "").replace(".json", "")
            try:
                with open(os.path.join(path, fn)) as _fh:      # closed deterministically: this walks a dir
                    d = json.load(_fh)
            except Exception:
                continue
            # VALID JSON IS NOT NECESSARILY AN OBJECT. A file containing a list, a string or a number
            # parses fine and then takes .get(), so one stray file aborted the whole intent-map load.
            if not isinstance(d, dict):
                continue
            ids = d.get("ids") or ([d["id"]] if d.get("id") else [])
            for bid in ids:
                m[bid] = intent
    elif os.path.exists(path):
        with open(path) as _fh:
            d = json.load(_fh)
        m = d if isinstance(d, dict) else {}
    return m


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent-map", help="JSON {batch_id: intent}, or a dir of *_batch_id.json files")
    ap.add_argument("--providers", default="openai,anthropic")
    a = ap.parse_args(argv)
    im = load_intent_map(a.intent_map) if a.intent_map else None
    print("Backfilling cost corpus from your batch ledgers (no spend)…")
    n, total = backfill(intent_map=im, providers=tuple(a.providers.split(",")))
    nodes, _ = learn.graph_stats()
    print(f"OK: {n} batch rows → calls (${total:,.2f} of historical spend); "
          f"{sum(c for _, c in nodes)} graph nodes.")
    if im:
        print(f"  tagged {sum(1 for v in im.values())} batch ids with intents from the map.")
    print("Next: `spendguard advise` / `spendguard backtest --as-of <date>`.")
    return 0
