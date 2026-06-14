"""Sync prices from LiteLLM's community-maintained dataset (breadth + freshness, ~zero maintenance).

LiteLLM's `model_prices_and_context_window.json` (CI-updated, ~2700 models, all providers) becomes the
breadth layer; spendguard's curated prices.json overrides verified models; the user can override on top.

`spendguard sync-prices` fetches → validates → caches to ~/.spendguard/litellm_prices.json. pricing.py
reads that cache (no network at import). A validation gate prevents caching a bad/empty fetch.
"""
import json, os, urllib.request, datetime
from . import config

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CACHE = str(config.HOME / "litellm_prices.json")


def _convert(raw):
    """LiteLLM per-token entries → spendguard per-1M rate dicts. Batch falls back to 50% of realtime."""
    models, provs = {}, {}
    for name, e in raw.items():
        if not isinstance(e, dict) or name.startswith("sample_"):
            continue
        ic = e.get("input_cost_per_token")
        if ic is None:
            continue
        try:
            ic = float(ic); oc = float(e.get("output_cost_per_token") or 0)
        except (TypeError, ValueError):
            continue
        cc = e.get("cache_read_input_token_cost")
        bi = e.get("input_cost_per_token_batches"); bo = e.get("output_cost_per_token_batches")
        rate = {
            "in_": ic * 1e6, "out": oc * 1e6,
            "cached_in": (float(cc) * 1e6 if cc is not None else ic * 1e6 * 0.1),
            "batch_in": (float(bi) * 1e6 if bi is not None else ic * 1e6 * 0.5),
            "batch_out": (float(bo) * 1e6 if bo is not None else oc * 1e6 * 0.5),
        }
        models[name] = {k: round(v, 6) for k, v in rate.items()}
        provs[name] = e.get("litellm_provider", "?")
    return models, provs


def _validate(models):
    """Sanity gate before trusting a fetch. (ok, [messages])."""
    if len(models) < 1000:
        return False, [f"only {len(models)} models parsed — suspicious; not caching"]
    msgs, checks = [], {"gpt-5.5": (5.0, 30.0), "claude-opus-4-8": (5.0, 25.0), "gpt-4o-mini": (0.15, 0.60)}
    for m, (ein, eout) in checks.items():
        r = models.get(m)
        if not r:
            msgs.append(f"note: {m} not in LiteLLM (curated value will be used)")
        elif abs(r["in_"] - ein) > 0.01 or abs(r["out"] - eout) > 0.01:
            msgs.append(f"DIFF {m}: LiteLLM {r['in_']}/{r['out']} vs verified {ein}/{eout} (curated wins)")
    return True, msgs


def sync():
    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "spendguard-sync/0.1"})
    raw = json.load(urllib.request.urlopen(req, context=config.ssl_context(), timeout=60))
    models, provs = _convert(raw)
    ok, msgs = _validate(models)
    if not ok:
        raise RuntimeError("LiteLLM data failed validation: " + "; ".join(msgs))
    out = {"_fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "_source": LITELLM_URL, "models": models, "providers": provs}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(out, open(CACHE, "w"))
    return len(models), msgs


def main(argv=None):
    print("Syncing prices from LiteLLM (community-maintained)…")
    try:
        n, msgs = sync()
    except Exception as e:
        print(f"sync failed: {e}\n  (existing cache, if any, is unchanged; curated prices still work.)")
        return 1
    print(f"OK: cached {n} models → {CACHE}")
    for m in msgs:
        print("  " + m)
    import importlib
    from . import pricing
    importlib.reload(pricing)
    by_prov = {}
    for p in pricing.PROVIDERS.values():
        by_prov[p] = by_prov.get(p, 0) + 1
    top = ", ".join(f"{k}:{v}" for k, v in sorted(by_prov.items(), key=lambda x: -x[1])[:10])
    print(f"pricing now knows {len(pricing.PRICING)} models across {len(by_prov)} providers ({top}).")
    print("Curated/verified models still take precedence over LiteLLM.")
    return 0
