"""Reconcile ACTUAL Anthropic (Claude/Opus 4.8) batch spend from real billed tokens.

Anthropic's batch LIST endpoint has no usage field, so we download each ended
batch's results and sum per-result usage (priced per-result by its own model via
canonical pricing.py, cache-aware). Per-batch sums are cached locally so daily
runs only fetch NEW batches (results expire 29d after creation).

ZERO paid calls (results download is free). Exempt from the spend gate.

LIMITATION: only BATCH usage is visible. Real-time Claude calls (e.g. the LOINC
Opus judge running via ThreadPool) are NOT captured — that needs an Admin API key
(/v1/organizations/cost_report). Flagged in output.

  python scripts/reconcile_anthropic_spend.py [--since YYYY-MM-DD] [--by-day]
"""
import os, sys, json, argparse, urllib.request, socket

from . import pricing
from .config import ANTHROPIC_CACHE as CACHE_PATH, api_key as _api_key
CACHE_PATH = str(CACHE_PATH)
socket.setdefaulttimeout(60)


def _key():
    k = _api_key("ANTHROPIC_API_KEY")
    if not k:
        sys.exit("ANTHROPIC_API_KEY not found")
    return k


def _h(k):
    return {"x-api-key": k, "anthropic-version": "2023-06-01"}


def _get(url, k):
    from .config import ssl_context
    return urllib.request.urlopen(urllib.request.Request(url, headers=_h(k)), context=ssl_context())


def list_batches(k):
    rows, after = [], None
    while True:
        u = "https://api.anthropic.com/v1/messages/batches?limit=100" + (f"&after_id={after}" if after else "")
        d = json.load(_get(u, k))
        rows.extend(d["data"])
        if d.get("has_more"):
            after = d["data"][-1]["id"]
        else:
            return rows


UNKNOWN_MODELS = {}  # model -> result count, for models missing from pricing.py (never guessed)


def _cost(model, u):
    """Cost for one result's usage dict, cache-aware, via pricing.py batch rates.
    Unknown model -> record + return 0 (never guess a price, never crash the report)."""
    try:
        p = pricing.price(model)
    except KeyError:
        UNKNOWN_MODELS[model] = UNKNOWN_MODELS.get(model, 0) + 1
        return 0.0
    fresh_in = u.get("input_tokens", 0)              # Anthropic input_tokens = uncached/fresh
    cread = u.get("cache_read_input_tokens", 0)
    ccreate = u.get("cache_creation_input_tokens", 0)
    out = u.get("output_tokens", 0)
    bin_, bout, bcache = p["batch_in"], p["batch_out"], p.get("cached_in", 0.0) * 0.5
    return (fresh_in * bin_ + cread * bcache + ccreate * bin_ * 1.25 + out * bout) / 1e6


def refresh_cache(k, cache):
    batches = list_batches(k)
    new = 0
    for b in batches:
        bid = b["id"]
        if bid in cache:
            continue
        if b.get("processing_status") != "ended" or not b.get("results_url"):
            continue
        try:
            lines = _get(b["results_url"], k).read().decode().splitlines()
        except Exception as e:
            print(f"  skip {bid}: {e}", flush=True)
            continue
        by_model = {}
        cost = 0.0
        for ln in lines:
            if not ln.strip():
                continue
            msg = json.loads(ln).get("result", {}).get("message", {})
            if not msg:
                continue
            mdl = pricing.normalize(msg.get("model", "claude-opus-4-8"))
            u = msg.get("usage", {})
            c = _cost(mdl, u)
            cost += c
            m = by_model.setdefault(mdl, {"in": 0, "out": 0, "cost": 0.0})
            m["in"] += u.get("input_tokens", 0); m["out"] += u.get("output_tokens", 0); m["cost"] += c
        cache[bid] = {"created_at": b["created_at"][:10], "cost": cost, "by_model": by_model}
        new += 1
        if new % 25 == 0:
            print(f"  ...cached {new} new batches", flush=True)
    if new:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        json.dump(cache, open(CACHE_PATH, "w"))
    return new


def cost_by_day(since=None):
    """Returns (by_day:{date:$}, by_model:{model:$}). Refreshes cache first."""
    k = _key()
    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}
    refresh_cache(k, cache)
    by_day, by_model = {}, {}
    for bid, rec in cache.items():
        d = rec["created_at"]
        if since and d < since:
            continue
        # RE-PRICE from stored token sums every call, so adding/fixing a price in
        # pricing.py corrects history without re-downloading results.
        for mdl, mm in rec.get("by_model", {}).items():
            try:
                c = pricing.batch_cost(mdl, mm.get("in", 0), mm.get("out", 0))
            except KeyError:
                UNKNOWN_MODELS[mdl] = UNKNOWN_MODELS.get(mdl, 0) + 1
                c = 0.0
            by_day[d] = by_day.get(d, 0.0) + c
            by_model[mdl] = by_model.get(mdl, 0.0) + c
    return by_day, by_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--by-day", action="store_true")
    a = ap.parse_args()
    by_day, by_model = cost_by_day(a.since)
    print(f"# Anthropic (Claude) BATCH spend — priced via pricing.py {pricing.PRICING_VERIFIED}")
    if a.since:
        print(f"# since {a.since} (UTC)")
    for mdl, c in sorted(by_model.items(), key=lambda x: -x[1]):
        print(f"  {mdl:<22} ${c:,.2f}")
    print(f"  {'TOTAL (batch only)':<22} ${sum(by_day.values()):,.2f}")
    print("  NOTE: real-time Claude calls NOT included (needs Admin cost_report key).")
    if UNKNOWN_MODELS:
        print("  WARN models missing from pricing.py (priced $0 — add them): "
              + ", ".join(f"{m}×{n}" for m, n in UNKNOWN_MODELS.items()))
    if a.by_day:
        for d in sorted(by_day):
            print(f"    {d}  ${by_day[d]:,.2f}")


if __name__ == "__main__":
    main()
