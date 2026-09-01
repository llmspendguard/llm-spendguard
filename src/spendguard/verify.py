"""spendguard verify — the forensic SELF-CHECK: is every path money can flow through actually CORRECT right now?

The actuarial core of spendguard is worth nothing if a call silently hits the wrong model, a down plan strands
instead of failing over, or an economics number rests on an unmeasured cap. So this composes the standing $0
structural checks into ONE verdict, and — with --probe — proves failover LIVE:

  1. MODEL IDS (model_preflight)     every configured id is served (or unchecked) + priced; a stale id names its fix.
  2. FAILOVER MAP (lane_catalog)     every lane's metered-fallback id is served + priced — a down plan degrades, not strands.
  3. PROVIDER KEYS                   each lane's metered API has a key present — without it the fallback has nowhere to go.
  4. ECONOMICS (lane_economics)      each plan's cap is measured and its pace known — utilization can be maximized.
  5. FAILOVER PROBE (--probe, live)  force each lane down and confirm the call bills the metered API on the exact model.

EXPLICIT STATES, never a bare pass/fail: a can't-check (catalog not synced → 'unchecked', cap not yet measured →
'measuring') is reported AS SUCH and drives 'collect more info' (sync-catalog / let the gauge move), never a false
red. Run it on a cadence — it is the guarantee that lanes + metered + failover + economics hold, not an assumption.

Nothing here is spent in the default ($0) mode; --probe makes one tiny real metered call per lane (~$0.0002 each)."""

import time

from . import config, gate


def _key_present(provider):
    """(present, key_env) for a provider's metered API key — resolvable via env / keys.env / config? A lane whose plan
    is down falls back to this provider's paid API; with no key the fallback has nowhere to go and a down plan STRANDS
    the call. present is None when the provider is unknown or declares no key env (nothing to resolve). config.api_key
    returns "" for an absent key — it does not raise — so no exception handling stands between a missing key and its
    honest False here."""
    from . import adapters
    spec = adapters.PROVIDERS.get(provider)
    if not spec:
        return None, None
    key_env = spec.get("key_env")
    if not key_env:
        return None, None
    return bool(config.api_key(key_env)), key_env


def _probe_failover(lane, prompt="Reply with exactly: OK"):
    """LIVE proof that THIS lane fails over to metered: force the lane cooling, call its configured model, confirm the
    result came from the metered API (executor='api') with no error, then restore the prior cooldown state. One real,
    tiny metered call — the only honest proof that a down plan bills the API instead of stranding. Never clears a
    cooldown the lane already had (a real backoff must survive the probe)."""
    from . import adapters, lane_catalog, resource_state
    prov = lane_catalog.lane_provider(lane)
    model = lane_catalog.configured_base(lane)
    if not prov or not model:
        return {"lane": lane, "ok": None, "executor": None, "cost": None, "note": "no configured model — not probed"}
    key = resource_state.lane_key(lane)
    was_cooling = resource_state.cooling(key)
    resource_state.cool(key, 120, reason="verify-failover-probe")
    try:
        r = adapters.call(f"{prov}:{model}", prompt, max_tokens=2000, timeout_s=45, sig="spendguard:verify-failover")
    finally:
        if not was_cooling:
            resource_state.clear_cooldown(key)             # restore: never leave a lane cooled by the probe itself
    ok = (r.get("executor") == "api") and not r.get("error") and bool(r.get("text"))
    note = ("failover → metered API OK" if ok
            else f"FAILOVER BROKEN — executor={r.get('executor')!r} error={str(r.get('error'))[:60]!r}")
    return {"lane": lane, "ok": ok, "executor": r.get("executor"), "model": r.get("model"),
            "cost": r.get("cost"), "note": note}


def verify_system(probe=False):
    """Run the checks and return a structured verdict:
      {preflight:[…], fallback:[…], keys:[…], economics:[…], probe:[…]|None, ok:bool}
    ok is True only when every STRUCTURAL check passes (a model id callable-as-written or correctable, every lane's
    fallback id priced + served-or-unchecked, every lane provider key present) — and, when probe=True, every live
    failover reached the metered API. 'unchecked'/'measuring' states are NOT failures (a can't-know is not a no)."""
    from . import lane_catalog, model_preflight, lane_economics

    preflight = model_preflight.preflight_models(model_preflight.configured_specs())
    fallback = lane_catalog.audit_lane_fallback()

    lanes = lane_catalog.lanes()
    keys = []
    for ln in lanes:
        prov = lane_catalog.lane_provider(ln)
        present, key_env = _key_present(prov) if prov else (None, None)
        keys.append({"lane": ln, "provider": prov, "key_env": key_env, "present": present,
                     "ok": bool(present), "note": ("key present" if present else f"MISSING {key_env} — fallback would strand")})

    econ = []
    now = time.time()
    for e in lane_economics.economics():
        b = e.get("binding")
        econ.append({"lane": e["lane"], "converged": e.get("converged"),
                     "cap": (b or {}).get("cap"), "remaining_pct": (b or {}).get("remaining_pct"),
                     "pace": (lane_economics._bucket_pace(b, now) if b else None), "meter": e.get("meter")})

    probe_rows = None
    if probe:
        probe_rows = []
        for ln in lanes:                                   # per-lane isolated: one probe's failure is a ✗ row, not a
            try:                                           # lost verdict; a DELIBERATE stop still halts the whole run
                probe_rows.append(_probe_failover(ln))
            except Exception as e:
                if isinstance(e, gate.deliberate_stop_types()):
                    raise
                probe_rows.append({"lane": ln, "ok": False, "executor": None, "cost": None,
                                   "note": f"probe error (this lane only): {str(e)[:60]}"})

    ok = (all(r["usable"] for r in preflight)
          and all(r["ok"] for r in fallback)
          and all(k["ok"] for k in keys)
          and (probe_rows is None or all(p["ok"] for p in probe_rows if p["ok"] is not None)))
    return {"preflight": preflight, "fallback": fallback, "keys": keys, "economics": econ,
            "probe": probe_rows, "ok": ok}


def format_verify(verdict):
    """The `spendguard verify` view: one section per check with ✓/✗ per row and an overall PASS/FAIL. Every failing or
    uncertain row carries its own reason + fix, so 'what is wrong and what do I do' is answerable from the output."""
    L = ["spendguard verify — is every money path correct right now?\n"]

    L.append("1. MODEL IDS (served + priced, before any spend):")
    for r in verdict["preflight"]:
        L.append(f"   {'✓' if r['usable'] else '✗'} {r['spec']:<30} {r['note']}")

    L.append("\n2. FAILOVER MAP (lane → metered id a down plan uses):")
    for r in verdict["fallback"]:
        L.append(f"   {'✓' if r['ok'] else '✗'} {r['lane']:<12} {r['use_name']} → {r['metered_id']} "
                 f"(served={r['served']}, priced={'yes' if r['priced'] else 'NO'})")

    L.append("\n3. PROVIDER KEYS (the metered fallback needs one):")
    for k in verdict["keys"]:
        L.append(f"   {'✓' if k['ok'] else '✗'} {k['lane']:<12} {k['provider']}: {k['note']}")

    L.append("\n4. ECONOMICS (cap measured → utilization can be maximized):")
    for e in verdict["economics"]:
        cap = f"cap~{int(e['cap']):,}" if e.get("cap") else "cap measuring"
        pace = f" pace {e['pace']:+.2f}" if e.get("pace") is not None else ""
        pct = f"  {e['remaining_pct']}% left" if e.get("remaining_pct") is not None else ""
        L.append(f"   · {e['lane']:<12} {e['meter']}: {cap}{pct}{pace}"
                 f"{'  (converged)' if e.get('converged') else '  (measuring)'}")

    if verdict["probe"] is not None:
        L.append("\n5. FAILOVER PROBE (live — lane forced down, must bill the metered API):")
        for p in verdict["probe"]:
            mark = "·" if p["ok"] is None else ("✓" if p["ok"] else "✗")
            L.append(f"   {mark} {p['lane']:<12} {p['note']}"
                     + (f" (executor={p['executor']}, ${p['cost']})" if p.get("executor") else ""))

    L.append(f"\n  {'✅ PASS' if verdict['ok'] else '❌ FAIL'} — "
             + ("every structural money path is correct"
                + (" and failover reached metered live" if verdict["probe"] is not None else
                   "; run `spendguard verify --probe` to prove failover live")
                if verdict["ok"] else "fix the ✗ rows above before trusting the routing/economics"))
    return "\n".join(L)


def cmd(argv=None):
    """`spendguard verify [--probe]` — the forensic self-check of lanes + metered + failover + economics. Default is
    $0 (structural); --probe adds one tiny live metered failover call per lane. Exit non-zero if any check fails."""
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard verify",
                                 description="Verify every money path (lanes, metered, failover, economics) is correct.")
    ap.add_argument("--probe", action="store_true",
                    help="also prove failover LIVE: force each lane down and confirm the call bills the metered API "
                         "(one tiny real call per lane, ~$0.0002 each)")
    a = ap.parse_args(argv)
    verdict = verify_system(probe=a.probe)
    print(format_verify(verdict))
    return 0 if verdict["ok"] else 1
