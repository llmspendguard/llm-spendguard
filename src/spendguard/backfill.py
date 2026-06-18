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
    cache = json.load(open(ra.CACHE_PATH)) if os.path.exists(ra.CACHE_PATH) else {}
    out = []
    for bid, rec in cache.items():
        for mdl, mm in rec.get("by_model", {}).items():
            try:
                cost = pricing.batch_cost(mdl, mm.get("in", 0), mm.get("out", 0))
            except Exception:
                cost = 0.0
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
        if bid in have:
            continue
        intent = intent_map.get(bid)
        cid = calls.insert(provider, model, "batch", cost, in_tok=it, out_tok=ot,
                           ts=ts, intent=intent, who="backfill:ledger")
        learn.add_node("run", f"{provider}:{model}",
                       attrs={"cost": round(cost, 4), "intent": intent, "date": ts, "call": cid, "batch": bid},
                       ts=ts, id=bid)
        total += cost
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
                d = json.load(open(os.path.join(path, fn)))
            except Exception:
                continue
            ids = d.get("ids") or ([d["id"]] if isinstance(d, dict) and d.get("id") else [])
            for bid in ids:
                m[bid] = intent
    elif os.path.exists(path):
        d = json.load(open(path))
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
