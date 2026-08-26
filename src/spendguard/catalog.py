"""Live model-catalog CACHE — the served-list store that lets the dispatch pre-flight ground a model id against
what a provider ACTUALLY serves right now ($0, no per-call latency), so a stale id (remembered from a past
session; Gemini rotates ids weekly) is caught AT dispatch instead of surfacing as a mystery provider 404.

This module is CACHE ONLY. The served-list PRIMITIVE and the pre-flight decision live in vendor_call
(serves / served_check / closest_served) — that is where a caller reads them; this file just keeps the list
fresh so served_check answers a served id from cache without a network call.

Mirrors sync.py (the price-breadth cache): a per-provider list of live model ids is fetched with a short TTL,
cached in SPENDGUARD_HOME/model_catalog.json, refreshed on the `saas sync` cadence and by `spendguard sync-catalog`.
The FETCH is not reinvented — it reuses vendor_call.list_models(), which GETs each provider's /models list ($0)
and returns ids in the DISPATCH form (its own `_dispatch_form` strips Gemini's `models/` listing prefix), so the
cached ids compare directly against a requested id. live_model_ids(vendor) returns the cached list, or None when
this vendor was never cached — None is 'not maintained here', which served_check treats as 'unchecked' (proceed),
never as 'no'.
"""
import os
import json
import datetime

from . import config

CATALOG_CACHE = str(config.HOME / "model_catalog.json")
DEFAULT_REFRESH_HOURS = 12          # ids rotate over days/weeks, not hours; 12h keeps it fresh without churn

_CACHE_MEM = {"mtime": None, "data": None}     # in-process memo, invalidated by file mtime (dispatch never re-reads disk needlessly)
_WARNED = set()                                 # warn at most once per provider per process on the "unknown" path


def pull_live_catalog(providers=None, timeout_s=20):
    """Fetch the live model-id list for every KEYED provider (or the given subset) via vendor_call.list_models and
    write the cache atomically. Returns (n_providers_ok, n_ids, errors_by_provider). Fail-soft PER provider: one
    provider's fetch error is recorded and the rest still cache — a partial catalog is better than none, and the
    error is surfaced, never silent."""
    from . import adapters, vendor_call
    provs = list(providers) if providers else sorted(adapters.PROVIDERS)
    models, errors, ceilings = {}, {}, {}
    for prov in provs:
        spec = adapters.PROVIDERS.get(prov)
        if not spec:
            errors[prov] = "unknown provider"
            continue
        if not config.api_key(spec.get("key_env") or ""):
            continue                            # no key → we don't call it, so we don't catalog it (not an error)
        try:
            res = vendor_call.list_models(prov, timeout_s=timeout_s)
        except Exception as e:
            errors[prov] = str(e)[:120]
            continue
        if res.get("error"):
            errors[prov] = str(res["error"])[:120]
            continue
        mods = res.get("models") or []
        ids = sorted({m.get("id") for m in mods if m.get("id")})   # list_models returns dispatch form
        if ids:
            models[prov] = ids
            # carry the vendor's own published OUTPUT ceiling where /models exposes one (deepseek/moonshot do;
            # OpenAI/Anthropic /models don't — pricing.max_output_tokens covers those). So the ceiling that clamps
            # a budget is IN the catalog, not a per-model auto-heal guess that poisons.
            c = {m["id"]: int(m["max_output_tokens"]) for m in mods if m.get("id") and m.get("max_output_tokens")}
            if c:
                ceilings[prov] = c
        else:
            errors[prov] = "provider returned an empty model list"
    out = {"_fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "models": models, "errors": errors, "ceilings": ceilings}
    config.update_json(CATALOG_CACHE, lambda _d: out)     # the one atomic writer (+~backup), like sync.CACHE
    _CACHE_MEM["mtime"] = None                             # force the memo to reload the freshly-written file
    return len(models), sum(len(v) for v in models.values()), errors


def _load_catalog():
    """The cached catalog dict, or None if absent/unreadable. Memoised by file mtime so the dispatch path reads
    disk only when the cache actually changed."""
    try:
        st = os.stat(CATALOG_CACHE)
    except OSError:
        return None
    if _CACHE_MEM["mtime"] != st.st_mtime:
        try:
            with open(CATALOG_CACHE) as f:
                _CACHE_MEM["data"] = json.load(f)
            _CACHE_MEM["mtime"] = st.st_mtime
        except Exception:
            return None
    return _CACHE_MEM["data"]


def live_model_ids(provider):
    """The provider's fresh live id list, or None when it is NOT positively known (no cache, or this provider is
    absent from the cache / its last fetch errored). None means 'cannot check', never 'no models' — the caller
    must treat it as unverifiable, not as a stale id."""
    data = _load_catalog()
    if not data:
        return None
    ids = (data.get("models") or {}).get(provider)
    return list(ids) if ids else None


def model_ceiling(vendor, model):
    """The vendor's published max OUTPUT tokens for a model, from the cached live /models listing — where the
    vendor exposes it (deepseek/moonshot include it; OpenAI/Anthropic /models do not, and pricing.max_output_tokens
    covers those). None when not known → the caller falls back to the synced limits cache, then heals. Ids are the
    dispatch form, so a requested id matches directly."""
    data = _load_catalog()
    if not data:
        return None
    c = (data.get("ceilings") or {}).get(vendor)
    if not isinstance(c, dict):
        return None
    v = c.get(model) or c.get(model.split(":", 1)[-1])
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def catalog_age_hours():
    """Age of the catalog cache in hours, or None if absent/unreadable (= needs a fetch)."""
    data = _load_catalog()
    try:
        ts = datetime.datetime.fromisoformat(data["_fetched"])
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600.0)
    except Exception:
        return None


def note_unknown_once(provider):
    """Emit the 'catalog not synced for this provider' notice at most once per provider per process (the
    warn+proceed path). Returns True the first time (so the caller can print), False after."""
    if provider in _WARNED:
        return False
    _WARNED.add(provider)
    return True


def refresh_catalog_if_stale():
    """Re-fetch the catalog only when older than catalog.refresh_hours (default 12; 0 disables), at the top of
    `saas sync` — the same no-dedicated-scheduler pattern as sync.refresh_if_stale for prices. Strictly fail-open:
    a failed fetch leaves the existing cache in effect and reports the error rather than raising."""
    try:
        hours = float(os.environ.get("SPENDGUARD_CATALOG_REFRESH_HOURS")
                      or config._cfg_get("catalog", "refresh_hours", DEFAULT_REFRESH_HOURS))
    except Exception:
        hours = float(DEFAULT_REFRESH_HOURS)
    if hours <= 0:
        return {"skipped": "catalog.refresh_hours=0"}
    age = catalog_age_hours()
    if age is not None and age < hours:
        return {"fresh": True, "age_hours": round(age, 2)}
    try:
        n_prov, n_ids, errors = pull_live_catalog()
        return {"refreshed": True, "providers": n_prov, "ids": n_ids, "errors": errors}
    except Exception as e:
        return {"error": str(e)[:120], "note": "existing catalog cache still in effect"}


def main(argv=None):
    """`spendguard sync-catalog` — refresh the live model catalog now and print a per-provider summary."""
    import sys
    print("Syncing live model catalogs from each keyed provider's /models list ($0, no generation)…")
    try:
        n_prov, n_ids, errors = pull_live_catalog()
    except Exception as e:
        print(f"  error: {e}", file=sys.stderr)
        return 1
    data = _load_catalog() or {}
    for prov, ids in sorted((data.get("models") or {}).items()):
        print(f"  {prov:12} {len(ids):4} live ids  (e.g. {', '.join(ids[:3])}…)")
    for prov, msg in sorted((data.get("errors") or {}).items()):
        print(f"  {prov:12} not cataloged: {msg}", file=sys.stderr)
    print(f"\n  {n_prov} providers, {n_ids} live model ids cached → {CATALOG_CACHE}")
    return 0
