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


# Per-unit billing fields passed through to the cache's `unit_models` section — pricing._load_units reads
# exactly these to price transcription ($/second), TTS ($/character) and flat-rate images ($/image). Unit-billed
# entries usually have NO input_cost_per_token, so they must be captured BEFORE the token-rate skip below.
_UNIT_COST_FIELDS = ("input_cost_per_second", "output_cost_per_second",
                     "input_cost_per_character", "output_cost_per_character",
                     "input_cost_per_image", "output_cost_per_image")


def _convert(raw):
    """LiteLLM per-token entries → spendguard per-1M rate dicts (+ per-UNIT entries passed through for
    _load_units: whisper $/s, tts $/char, dall-e $/image). Batch falls back to 50% of realtime."""
    models, provs, unit_models = {}, {}, {}
    for name, e in raw.items():
        if not isinstance(e, dict) or name.startswith("sample_"):
            continue
        u = {k: float(e[k]) for k in _UNIT_COST_FIELDS if e.get(k) is not None}
        if u:
            unit_models[name] = u
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
    return models, provs, unit_models


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
    models, provs, unit_models = _convert(raw)
    ok, msgs = _validate(models)
    if not ok:
        raise RuntimeError("LiteLLM data failed validation: " + "; ".join(msgs))
    out = {"_fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "_source": LITELLM_URL, "models": models, "providers": provs, "unit_models": unit_models}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(out, open(CACHE, "w"))
    return len(models), msgs


DEFAULT_REFRESH_DAYS = 1   # pricing.refresh_days default: the LiteLLM dataset is CI-updated, daily is plenty


def cache_age_days():
    """Age of the LiteLLM price cache in days, or None if absent/unreadable (= needs a fetch)."""
    try:
        ts = datetime.datetime.fromisoformat(json.load(open(CACHE))["_fetched"])
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 86400.0)
    except Exception:
        return None


def refresh_if_stale():
    """Keep the price table fresh WITHOUT a dedicated scheduler: called at the top of every `saas sync` (which the
    installed `spendguard schedule` agent already runs on a cadence), it re-fetches only when the cache is older
    than `pricing.refresh_days` (default 1; 0 disables) — so an hourly scheduler still refreshes at most once a
    day. Strictly fail-open: a failed fetch leaves the existing cache + curated prices.json in effect and reports
    the error instead of raising. On success the in-process pricing table is reloaded so THIS run (reconcile's
    batch pricing, unit/ft: lookups) already uses the fresh rates."""
    from . import config
    try:
        days = float(os.environ.get("SPENDGUARD_PRICES_REFRESH_DAYS")
                     or config._cfg_get("pricing", "refresh_days", DEFAULT_REFRESH_DAYS))
    except Exception:
        days = float(DEFAULT_REFRESH_DAYS)
    if days <= 0:
        return {"skipped": "pricing.refresh_days=0"}
    age = cache_age_days()
    if age is not None and age < days:
        return {"fresh": True, "age_days": round(age, 2)}
    try:
        n, msgs = sync()
        import importlib
        from . import pricing
        importlib.reload(pricing)                       # same in-place reload main() uses — existing refs stay valid
        return {"refreshed": True, "models": n, "notes": msgs[:3]}
    except Exception as e:
        return {"error": str(e)[:120], "note": "existing cache + curated prices.json still in effect"}


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
