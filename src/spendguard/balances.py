"""Per-vendor METERED prepay/credit balance — "how much is available", so routing can prefer idle sunk credit.

Two kinds of metered credit, and the difference is the whole point:
  • SUNK POOL   — manual prepay, no auto-refill (measured: deepseek, moonshot). The money is ALREADY spent;
                  idle or expiring pool is pure loss, so it is worth DRAWING DOWN with substitutable work.
  • ON-DEMAND   — auto-top-up / pay-as-you-go. Every dollar is FRESH money when charged, so there is no
                  "use it before it expires" incentive; a low balance here means an imminent recharge.
Auto-top-up is not exposed by the balance APIs (it lives behind a billing-session key), so which vendors are
on-demand is USER-DECLARED (config balances.pools[vendor].auto_topup) — never guessed.

DISCOVERY, not hardcoding: the available number comes from each vendor's own balance endpoint (config
balances.endpoints; measured defaults for the vendors that expose one), and WHICH field is the spendable
balance is an AGENTIC read (deepseek reports balance_infos[].total_balance, moonshot data.available_balance,
a new vendor something else — "which is spendable, and is any of it expiring" is a judgement, so an LLM decides
it, never a per-vendor field map). Fail-open where a vendor exposes no balance / the fetch fails / the read
fails: source 'unknown', never an invented number — a can't-check is not a zero.

Mirrors sync.py / catalog.py: a short-TTL cache in SPENDGUARD_HOME/balances.json, refreshed on the `saas sync`
cadence and by `spendguard balances`. The agentic read is cached per (vendor, raw-hash), so an unchanged balance
never re-pays for the extraction. Billed axis: the balance FETCH is $0 (a GET); the one-time read per refresh is
a few tenths of a cent of metered LLM, recorded under intent spendguard:balance-read.
"""
import os
import json
import hashlib
import datetime

from . import config

BALANCE_CACHE = str(config.HOME / "balances.json")
DEFAULT_REFRESH_HOURS = 6            # balances move with usage → shorter TTL than the model catalog

# Measured 2026-08-26 to actually return a balance with a normal API key. URL + auth style per vendor; this is
# provider CONFIG (endpoints, like PROVIDERS base_url), overridable/extendable via config balances.endpoints —
# not a hardcoded balance VALUE. A vendor absent here (openai/anthropic/gemini: no balance with a plain key) has
# no API balance → 'unknown', never a zero.
_ENDPOINTS_DEFAULT = {
    "deepseek": {"url": "https://api.deepseek.com/user/balance", "auth": "bearer"},
    "moonshot": {"url": "https://api.moonshot.ai/v1/users/me/balance", "auth": "bearer"},
}

_BALANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "available": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
        "expiring": {"type": ["number", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["available", "currency"],
}
_BALANCE_SYS = ("You read one vendor's account-balance API response and report the balance a caller can SPEND "
                "right now. Return the single spendable AVAILABLE amount (not a lifetime total of top-ups, not a "
                "used/consumed figure), its currency, and — only if the response states it — the amount that will "
                "EXPIRE (else null). If the response carries no spendable balance, available is null. Never invent "
                "a number; report only what the JSON supports.")


def _endpoints():
    """Vendor→{url,auth} balance endpoints: the measured defaults, overlaid with config balances.endpoints (so a
    new vendor's endpoint is added by CONFIG, never a code edit)."""
    out = dict(_ENDPOINTS_DEFAULT)
    cfg = config._cfg_get("balances", "endpoints", None)
    if isinstance(cfg, dict):
        out.update(cfg)
    return out


def _pool_meta(vendor):
    """User-declared pool metadata for a vendor: {auto_topup: bool, expiry: 'YYYY-MM-DD'|None} from config
    balances.pools. Auto-top-up can't be read from the balance API, so the operator states it; absent = a plain
    sunk pool with no known expiry."""
    pools = config._cfg_get("balances", "pools", None) or {}
    m = pools.get(vendor) if isinstance(pools, dict) else None
    return m if isinstance(m, dict) else {}


def fetch_balance_raw(vendor, timeout_s=15):
    """The vendor's raw balance JSON (dict), or None when there is no endpoint / no key / the GET fails. $0 — a
    GET, no generation. Never raises: a discovery failure is a None (can't-check), not an exception."""
    ep = _endpoints().get(vendor)
    if not ep:
        return None
    from . import adapters
    spec = adapters.PROVIDERS.get(vendor) or {}
    key = config.api_key(spec.get("key_env") or "")
    if not key:
        return None
    hdr = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if ep.get("auth") == "xapi"
           else {"Authorization": "Bearer " + key})
    try:
        import urllib.request
        req = urllib.request.Request(ep["url"], headers=hdr)
        body = urllib.request.urlopen(req, context=config.ssl_context(), timeout=timeout_s).read().decode()
        d = json.loads(body)
        return d if isinstance(d, dict) else {"_raw": d}
    except Exception:
        return None


def extract_available(vendor, raw, run=True):
    """{available, currency, expiring, note} read AGENTICALLY from a vendor's raw balance JSON — which field is
    the spendable balance is a judgement that differs per vendor, so an LLM decides it (never a per-vendor field
    map). run=False estimates only (no spend). On any failure returns available=None (source stays 'unknown' —
    never a fabricated number). The WHOLE response is shown to the model (a balance body is tiny; never truncate
    what an LLM must read, or the balance/expiry field could be the part cut off)."""
    from . import adapters, calls
    prompt = (f"Vendor: {vendor}\nIts account-balance API returned this JSON:\n{json.dumps(raw)}\n\n"
              "Report the spendable available balance now.")
    try:
        if not run:
            return {"available": None, "currency": None, "expiring": None, "note": "estimate-only"}
        with calls.context(intent="spendguard:balance-read"):
            # NO hardcoded cap: the reply IS content (the balance number), so a literal max_tokens would be a cap on
            # a content call — the exact truncate-to-wrong-number defect. The sig lets _call_guarded floor + measure
            # the budget and clamp it to the model ceiling, so a tiny JSON reply is never cut off.
            r = adapters.call(config.advisor_model(), prompt, system=_BALANCE_SYS,
                              schema=_BALANCE_SCHEMA, sig="spendguard:balance-read")
        if r.get("error"):
            return {"available": None, "currency": None, "expiring": None, "note": f"read failed: {r.get('error')}"}
        import re
        blob = re.search(r"\{.*\}", r.get("text") or "", re.S)   # PARSE the JSON envelope; the model already decided
        return json.loads(blob.group(0)) if blob else {"available": None, "currency": None, "expiring": None, "note": "no json"}
    except Exception as e:
        return {"available": None, "currency": None, "expiring": None, "note": f"read error: {str(e)[:80]}"}


def _load_cache():
    try:
        with open(BALANCE_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def vendor_balance(vendor, refresh=False):
    """{vendor, available, currency, source, kind, expiring, auto_topup, expiry, as_of} for one vendor.
      source 'api'       — the vendor's balance endpoint answered and the agentic read produced a number;
      source 'api-stale' — a cached number is kept because the endpoint was momentarily unreachable this refresh;
      source 'unknown'   — no endpoint / no key / fetch failed / read failed → available None (never a zero).
      kind   'on_demand' when the operator declared auto-top-up (fresh spend, no idle-pool incentive), else
             'sunk_pool' (manual prepay worth drawing down) — only meaningful when there IS an available number.
    Cached; the agentic read is skipped when the raw balance is byte-identical to the cached one (raw-hash)."""
    cache = _load_cache()
    cached = (cache.get("vendors") or {}).get(vendor) or {}
    meta = _pool_meta(vendor)
    auto = bool(meta.get("auto_topup"))
    raw = fetch_balance_raw(vendor) if (refresh or not cached) else cached.get("_raw")
    if raw is None:
        # keep a prior good number if we merely couldn't re-reach the endpoint this time, but flag the source
        avail = cached.get("available")
        return {"vendor": vendor, "available": avail, "currency": cached.get("currency"),
                "expiring": cached.get("expiring"), "as_of": cached.get("as_of"),
                "source": "api-stale" if avail is not None else "unknown",
                "kind": ("on_demand" if auto else "sunk_pool") if avail is not None else "unknown",
                "auto_topup": auto, "expiry": meta.get("expiry")}
    rawhash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]
    if cached.get("_rawhash") == rawhash and cached.get("available") is not None:
        ext = {k: cached.get(k) for k in ("available", "currency", "expiring")}   # unchanged → reuse the read ($0)
    else:
        ext = extract_available(vendor, raw)
    avail = ext.get("available")
    return {"vendor": vendor, "available": avail, "currency": ext.get("currency"), "expiring": ext.get("expiring"),
            "source": "api" if avail is not None else "unknown",
            "kind": ("on_demand" if auto else "sunk_pool") if avail is not None else "unknown",
            "auto_topup": auto, "expiry": meta.get("expiry"),
            "as_of": cached.get("as_of"), "_raw": raw, "_rawhash": rawhash, "_note": ext.get("note")}


def refresh_balances(vendors=None):
    """Re-fetch + re-read every keyed vendor's balance and write the cache atomically. Returns the per-vendor
    result list. $0 fetches; the agentic read runs only for a vendor whose raw balance changed."""
    provs = list(vendors) if vendors else sorted(_endpoints())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    out = {}
    for v in provs:
        b = vendor_balance(v, refresh=True)
        b["as_of"] = now
        out[v] = b
    config.update_json(BALANCE_CACHE, lambda _d: {"_fetched": now, "vendors": out})
    return list(out.values())


def cache_age_hours():
    try:
        ts = datetime.datetime.fromisoformat(_load_cache()["_fetched"])
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 3600.0)
    except Exception:
        return None


def refresh_balances_if_stale():
    """Re-read balances only when older than balances.refresh_hours (default 6; 0 disables), at the top of
    `saas sync` — the same no-dedicated-scheduler pattern as prices/catalog (uniquely named, not the bare
    refresh_if_stale that sync.py already owns). Fail-open: an error leaves the existing cache in effect and is
    reported, never raised."""
    try:
        hours = float(os.environ.get("SPENDGUARD_BALANCES_REFRESH_HOURS")
                      or config._cfg_get("balances", "refresh_hours", DEFAULT_REFRESH_HOURS))
    except Exception:
        hours = float(DEFAULT_REFRESH_HOURS)
    if hours <= 0:
        return {"skipped": "balances.refresh_hours=0"}
    age = cache_age_hours()
    if age is not None and age < hours:
        return {"fresh": True, "age_hours": round(age, 2)}
    try:
        rows = refresh_balances()
        return {"refreshed": True, "vendors": len(rows)}
    except Exception as e:
        return {"error": str(e)[:120], "note": "existing balances cache still in effect"}


def all_balances(refresh=False):
    """Per-vendor balance for every configured endpoint, from cache (or a live refresh)."""
    if refresh:
        return refresh_balances()
    cache = _load_cache()
    return list((cache.get("vendors") or {}).values()) or refresh_balances()


def main(argv=None):
    """`spendguard balances` — show available metered credit per vendor, split sunk-pool vs on-demand."""
    import sys
    fresh = not (argv and "--cached" in argv)
    print("Reading per-vendor metered balances (endpoint GET = $0; the field-read is a tiny cached LLM call)…")
    rows = all_balances(refresh=fresh)
    print(f"\n  {'vendor':10} {'available':>14}  {'kind':10} {'source':10} expiry")
    sunk = 0.0
    for b in sorted(rows, key=lambda r: (r.get("kind") or "z", -(r.get("available") or 0))):
        amt = b.get("available")
        cur = (b.get("currency") or "").upper()
        disp = f"{amt:,.2f} {cur}" if amt is not None else "unknown"
        exp = b.get("expiry") or (f"expiring {b.get('expiring')}" if b.get("expiring") else "")
        print(f"  {b['vendor']:10} {disp:>14}  {b.get('kind') or '—':10} {b.get('source') or '—':10} {exp}")
        if b.get("kind") == "sunk_pool" and amt and (cur in ("USD", "")):
            sunk += amt
    print(f"\n  idle sunk-pool credit (USD, worth drawing down first): ${sunk:,.2f}")
    print("  note: 'on_demand' vendors auto-top-up (fresh spend on use); declare them via config balances.pools.",
          file=sys.stderr)
    return 0
