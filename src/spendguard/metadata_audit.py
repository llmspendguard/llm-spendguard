"""Health + drift audit of the MODEL-METADATA backbone — the published limits spendguard clamps to, and the
measured caps it raises within them. The guard that would have caught two real, silent failures:

  1. THE EMPTY CACHE. `pricing.max_output_tokens()` reads limits synced from LiteLLM's community-maintained
     `model_prices_and_context_window.json` (sync.py). Measured 2026-08-14 the synced cache held ZERO models,
     so max_output_tokens() returned None for every model and `output_cap`'s "clamp to the published max"
     silently became a no-op — the 32K floor carrying the whole load with no upper bound. Nothing flagged it,
     because a None limit reads as "no opinion", not "the table is empty". This audit FAILS on an empty/stale
     cache instead of letting it pass as healthy.

  2. STALE MEASURED CAPS (the kimi shape). A cap in the measured registry (output_caps.json) that sits BELOW
     the model's published output max is drift — a stale probe starving a model that finishes longer now. The
     audit flags every measured cap under its published ceiling. It deliberately does NOT flag "measured, no
     published": a brand-new model (kimi-k3, glm-5.3) that LiteLLM's table has not caught up to is EXPECTED to
     have only a measured cap, and the 32K floor protects it. Measured-below-published is drift; measured-only
     is fine.

This is a mechanical audit (freshness dates and integer comparisons on a fixed data shape — parsing, not
judgement), so it is code, not an LLM call. It reports UNKNOWN separately from OK: an absent published max is
"cannot compare", never "clean".

    spendguard metadata            # print the backbone report
    spendguard metadata --sync     # refresh the LiteLLM cache first, then report
    from spendguard import metadata_audit; r = metadata_audit.backbone_health()   # {ok, cache, drift, ...}
"""
import argparse

from . import pricing, sync, vendor_call

# Named thresholds (values with a name; the audit's own limits, not call-site literals).
MODEL_METADATA_STALE_DAYS = 14     # a limits cache older than this is stale enough to alarm (sync refreshes daily)
MIN_EXPECTED_MODELS = 1000         # the LiteLLM table carries ~2,500; far under this means a bad/partial sync


def _cache_health():
    """Freshness + breadth of the synced LiteLLM limits cache. ok=False on absent/empty/stale — the states that
    silently disable the published-max clamp."""
    age = sync.cache_age_days()                         # None when absent/unreadable
    models = len(pricing.CONTEXT_LIMITS or {})
    present = age is not None and models > 0
    stale = present and age is not None and age > MODEL_METADATA_STALE_DAYS
    thin = present and models < MIN_EXPECTED_MODELS
    ok = present and not stale and not thin
    reasons = []
    if not present:
        reasons.append("the LiteLLM limits cache is ABSENT or EMPTY — max_output_tokens()/max_input_tokens() "
                       "return None for every model, so output_cap cannot clamp to the published max. "
                       "Run `spendguard sync-prices`.")
    if stale:
        reasons.append(f"cache is {age:.1f} days old (> {MODEL_METADATA_STALE_DAYS}) — new models/limit "
                       f"changes are likely missing. Run `spendguard sync-prices`.")
    if thin:
        reasons.append(f"cache holds only {models} models (< {MIN_EXPECTED_MODELS}) — a partial/failed sync.")
    return {"ok": ok, "present": present, "age_days": (round(age, 2) if age is not None else None),
            "models": models, "stale": stale, "thin": thin, "reasons": reasons}


def _cap_drift():
    """Every measured output cap vs the model's published max. Returns (drift, unknown):
       drift   = measured < published  → stale probe starving a model (the kimi shape)
       unknown = no published max      → cannot compare; measured is the sole authority (a too-new model). Not a fault."""
    drift, unknown = [], []
    caps = vendor_call.caps() or {}
    for key, rec in caps.items():
        if not isinstance(rec, dict):
            continue
        measured = rec.get("max_output_tokens")
        if not measured:
            continue
        model = key.split("/", 1)[-1]                   # 'moonshot/kimi-k3' -> 'kimi-k3'
        published = pricing.max_output_tokens(model)
        row = {"key": key, "model": model, "measured": int(measured),
               "published": (int(published) if published else None), "method": rec.get("method"),
               "measured_at": rec.get("measured_at")}
        if published and int(measured) < int(published):
            drift.append(row)
        elif not published:
            unknown.append(row)
    return drift, unknown


def backbone_health():
    """The full report: {ok, cache:{...}, drift:[...], unknown:[...]}. ok=False when the cache is unhealthy OR
    any measured cap has drifted below its published ceiling — the two states that silently degrade output_cap."""
    cache = _cache_health()
    drift, unknown = _cap_drift()
    return {"ok": bool(cache["ok"] and not drift), "cache": cache, "drift": drift, "unknown": unknown}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Model-metadata backbone health + measured-cap drift audit.")
    ap.add_argument("--sync", action="store_true", help="refresh the LiteLLM limits cache before auditing")
    a = ap.parse_args(argv)
    if a.sync:
        try:
            n, _ = sync.sync()
            import importlib
            importlib.reload(pricing)
            print(f"synced LiteLLM limits: {n} models cached.")
        except Exception as e:
            print(f"sync failed: {str(e)[:120]} (auditing the existing cache).")
    r = backbone_health()
    c = r["cache"]
    print(f"\nmodel-metadata backbone: {'OK' if r['ok'] else 'NEEDS ATTENTION'}")
    print(f"  LiteLLM limits cache: {c['models']} models, "
          f"age {c['age_days']}d — {'ok' if c['ok'] else 'PROBLEM'}")
    for msg in c["reasons"]:
        print(f"    ! {msg}")
    if r["drift"]:
        print("  measured caps BELOW published (drift — stale probe starves the model):")
        for d in r["drift"]:
            print(f"    ! {d['key']}: measured {d['measured']:,} < published {d['published']:,}  "
                  f"[{d['method']}, {d['measured_at']}] — re-probe or delete the stale cap")
    else:
        print("  measured caps vs published: no drift (none sit below their published max)")
    if r["unknown"]:
        print("  measured-only (no published max yet — too new for LiteLLM; 32K floor protects, not a fault):")
        for u in r["unknown"]:
            print(f"    · {u['key']}: measured {u['measured']:,} [{u['method']}]")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
