"""Lane + metered reachability sweep — prove every subscription lane ($0) and every keyed metered provider can
actually SERVE a call, so the routing layer has a reliable, current picture of what is up.

$0 for the lanes (the existing print-mode probe). Metered: ONE tiny call per keyed provider, to a cheap chat
model. The target per provider is config reliability.probe_models[provider], else a named cheap-chat default,
else the cheapest LIVE model with OUTPUT pricing derived from the served catalog (embeddings/inputs-only priced
models fall out naturally). The default map is a config DEFAULT (overridable), and every target is checked against
the live catalog at dispatch, so a rotated id is caught, not sent blind.

ESTIMATE-FIRST (the spend protocol): sweep(run=False) returns the plan + a $ estimate and spends NOTHING;
sweep(run=True) executes and returns the reachability matrix — per resource {reachable, executor, cost, reason}.
"""
from . import adapters, config

# A cheap CHAT model per provider for the probe — a config DEFAULT (reliability.probe_models overrides), not a
# hardcoded truth: it is validated against the live catalog at dispatch, and falls back to a derived cheapest.
_PROBE_DEFAULTS = {
    "openai": "gpt-5-nano", "anthropic": "claude-haiku-4-5", "gemini": "gemini-flash-latest",
    "deepseek": "deepseek-chat", "zai": "glm-4.6", "moonshot": "kimi-k2.6", "qwen": "qwen-flash",
}
_PROBE_IN, _PROBE_OUT = 12, 8          # a one-line probe prompt + a one-word reply


def _metered_target(provider):
    """The model to probe a provider's metered API: config override → named cheap-chat default (if the live
    catalog serves it) → the cheapest LIVE model with OUTPUT pricing (a chat model; embeddings have no output
    price). None when the provider has no usable target. Derived, never a blind hardcode."""
    ov = config._cfg_get("reliability", "probe_models", None)
    if isinstance(ov, dict) and ov.get(provider):
        return ov[provider]
    from . import catalog, pricing
    live = set(catalog.live_model_ids(provider) or [])
    default = _PROBE_DEFAULTS.get(provider)
    if default and (not live or default in live):        # trust the default unless the catalog positively lacks it
        return default
    best = None
    for mid in sorted(live):
        try:
            out = pricing.price(f"{provider}:{mid}").get("out")
        except Exception:
            out = None
        if out and (best is None or out < best[0]):
            best = (out, mid)
    return best[1] if best else default


def plan():
    """{lanes: [(lane, model)], metered: [(provider, model)]} — every configured lane + every keyed metered
    provider with a derivable probe target."""
    lane_models = config._cfg_get("advisor", "lane_models", {}) or {}
    lanes = [(lane, lane_models.get(lane)) for _p, (lane, _m) in sorted(adapters._LANES.items())]
    metered = []
    for prov in sorted(adapters.PROVIDERS):
        if not config.api_key((adapters.PROVIDERS[prov].get("key_env") or "")):
            continue
        t = _metered_target(prov)
        if t:
            metered.append((prov, t))
    return {"lanes": lanes, "metered": metered}


def sweep_estimate(pl=None):
    """Zero-spend $ estimate of the metered half (the lanes are $0). Tiny prompt + reply per provider."""
    from . import pricing
    pl = pl or plan()
    rows, total = [], 0.0
    for prov, mid in pl["metered"]:
        try:
            c = pricing.realtime_cost(f"{prov}:{mid}", _PROBE_IN, _PROBE_OUT)
        except Exception:
            c = None
        rows.append((prov, mid, c))
        total += (c or 0.0)
    return {"metered_cost": total, "rows": rows, "n_lanes": len(pl["lanes"]), "n_metered": len(pl["metered"])}


def sweep(run=False):
    """The reachability matrix. run=False → estimate only ($0). run=True → probe every lane ($0, existing probe)
    + every metered provider (tiny spend) → {resource: {reachable, executor, cost, reason}}."""
    pl = plan()
    out = {"estimate": sweep_estimate(pl), "lanes": {}, "metered": {}}
    if not run:
        return out
    from . import lanes as _lanes
    for r in _lanes.probe():                              # $0 subscription probe, one per enabled lane
        out["lanes"][r["lane"]] = {"reachable": bool(r.get("ok")), "cost": 0.0,
                                   "reason": r.get("error"), "latency": r.get("latency")}
    for prov, mid in pl["metered"]:                      # a tiny metered call per provider, through the gate. No
        # max_tokens literal: it is a reachability ping (only `error` is read, the reply is discarded), the model
        # emits ~a word regardless of the cap, and the sig lets _call_guarded size + ceiling-clamp the budget.
        r = adapters.call(f"{prov}:{mid}", "Reply with one word: ok.", sig="spendguard:reliability-sweep")
        out["metered"][prov] = {"model": mid, "reachable": not r.get("error"), "cost": r.get("cost"),
                                "executor": r.get("executor"), "reason": r.get("error_type") or r.get("error")}
    return out


def main(argv=None):
    argv = list(argv or [])
    run = "--run" in argv
    est = sweep_estimate()
    print(f"Reliability sweep — {est['n_lanes']} lanes ($0 probe) + {est['n_metered']} metered providers "
          f"(estimate ~${est['metered_cost']:.4f} total, tiny per-provider):")
    for prov, mid, c in est["rows"]:
        print(f"  metered {prov:10} {mid:28} ~${(c or 0):.5f}")
    if not run:
        print("\n  estimate only — re-run `spendguard reliability --run` to execute the live sweep.")
        return 0
    res = sweep(run=True)
    print("\n  LANE reachability ($0 subscription):")
    for lane, d in sorted(res["lanes"].items()):
        print(f"    {lane:12} " + ("🟢 LIVE" if d["reachable"] else "🔴 " + str(d.get("reason"))[:60])
              + (f"  ({d.get('latency')}s)" if d.get("latency") else ""))
    print("\n  METERED reachability:")
    n_ok = 0
    for prov, d in sorted(res["metered"].items()):
        n_ok += 1 if d["reachable"] else 0
        print(f"    {prov:10} " + ("🟢" if d["reachable"] else "🔴") + f" {d['model']:26} "
              + (f"cost=${(d.get('cost') or 0):.6f} exec={d.get('executor')}" if d["reachable"]
                 else f"reason={str(d.get('reason'))[:50]}"))
    print(f"\n  {sum(1 for d in res['lanes'].values() if d['reachable'])}/{len(res['lanes'])} lanes + "
          f"{n_ok}/{len(res['metered'])} metered providers reachable.")
    return 0
