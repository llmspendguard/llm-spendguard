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
import hashlib

from . import adapters, config

# A cheap CHAT model per provider for the probe — a config DEFAULT (reliability.probe_models overrides), not a
# hardcoded truth: it is validated against the live catalog at dispatch, and falls back to a derived cheapest.
_PROBE_DEFAULTS = {
    "openai": "gpt-5-nano", "anthropic": "claude-haiku-4-5", "gemini": "gemini-flash-latest",
    "deepseek": "deepseek-chat", "zai": "glm-4.6", "moonshot": "kimi-k2.6", "qwen": "qwen-flash",
}
_PROBE_IN, _PROBE_OUT = 12, 8          # a one-line probe prompt + a one-word reply


def _metered_target(provider):
    """The model to probe a provider's metered API: config override → a model THIS install actually depends on for
    this provider → named cheap-chat default (if the live catalog serves it) → the cheapest LIVE model with OUTPUT
    pricing. None when the provider has no usable target. Derived, never a blind hardcode."""
    ov = config._cfg_get("reliability", "probe_models", None)
    if isinstance(ov, dict) and ov.get(provider):
        return ov[provider]
    # PREFER a model the user's config actually calls for this provider (advisor.model/judge/tiers/lane_models), so
    # the check verifies the ids you DEPEND on (e.g. kimi-k3) rather than merely the cheapest one served. Explicit
    # `reliability.probe_models` still wins above.
    try:
        from . import model_preflight, gate as _g
        for spec in model_preflight.configured_specs():
            if _g._provider_of(spec) == provider:
                return spec.split(":", 1)[-1]
    except Exception:
        pass
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


_PROBE_OUT_TOKENS = 64          # a reachability ping only needs room for "ok" — deliberately tiny so a reasoning model's probe stays fast


def sweep(run=False, timeout_s=20):
    """The reachability matrix. run=False → estimate only ($0). run=True → probe every lane ($0) + every metered
    provider (a tiny gated ping) → {resource: {reachable, executor, cost, reason, latency}}. Each probe is BOUNDED
    by `timeout_s`, so ONE hung endpoint fails in ~timeout_s instead of its full work-timeout (the agy lane's 300s
    was exactly that wedge). The lane probes run concurrently + persisted inside lanes.probe; the metered pings are a
    short sequential pass (a handful of providers, pennies total — cheap to re-run, so no checkpoint is warranted)."""
    import time as _t
    pl = plan()
    out = {"estimate": sweep_estimate(pl), "lanes": {}, "metered": {}}
    if not run:
        return out
    from . import lanes as _lanes
    for r in _lanes.probe(timeout_s=timeout_s):          # $0 subscription probe — bounded + concurrent inside lanes.probe
        if r.get("skipped"):
            continue                                     # a lane the executor did not enable is not a reachability row
        out["lanes"][r["lane"]] = {"reachable": bool(r.get("ok")), "cost": 0.0,
                                   "reason": r.get("error"), "latency": r.get("latency")}
    for prov, mid in pl["metered"]:                      # a tiny metered call per provider, through the gate.
        # A REACHABILITY ping reads only `error` (the reply is discarded), so it is a PROBE: _probe=True keeps it a
        # single tiny shot — it is NOT floored to reasoning headroom and does NOT grow on an empty reply, so a heavy
        # REASONING model (kimi-k3 at high effort) answers the probe in seconds instead of reasoning through a 32k
        # budget. An empty-but-error-free reply still reads as reachable. timeout_s bounds a dead provider.
        t0 = _t.time()
        r = adapters.call(f"{prov}:{mid}", "Reply with one word: ok.", sig="spendguard:reliability-sweep",
                          max_tokens=_PROBE_OUT_TOKENS, timeout_s=timeout_s, _probe=True)
        # A reachability probe answers "is the endpoint up + authed?". adapters' TYPED `truncated` flag means the
        # model produced MORE than the tiny probe cap — i.e. it ANSWERED (a verbose model overruns "ok") — so the
        # endpoint is REACHABLE. Reading that structured boolean is parsing a known field, not judging the reply.
        _err = r.get("error")
        _truncated = r.get("truncated") is True
        out["metered"][prov] = {"model": mid, "reachable": (not _err) or _truncated, "cost": r.get("cost"),
                                "executor": r.get("executor"), "latency": round(_t.time() - t0, 2),
                                "reason": None if _truncated else (r.get("error_type") or _err)}
    return out


_REMEDIATE_SYS = (
    "You diagnose why a spendguard LLM LANE (a subscription CLI: codex/claude-code/gemini/zai) or a metered "
    "PROVIDER is unreachable, and tell the USER the exact fix. Given the resource and its error string, return: "
    "issue (one line — what is actually wrong), fix (one line — what the user must do), command (the exact shell "
    "command or console action, or '' if none). Known patterns: OAuth/session expired or 'could not be refreshed' "
    "→ re-login (claude-code: `claude setup-token`; codex: `codex login`; gemini/agy: re-auth the agy CLI); "
    "credits/prepayment depleted or a billing 402/429-credit → top up that provider's billing console; an API "
    "'not enabled'/'has not been used in project' → enable it (e.g. `gcloud services enable aiplatform.googleapis"
    ".com --project=<id>`); a plain rate limit (429 no-credit) → transient, it retries with backoff, no user "
    "action; an 'invalid/unknown model' → the configured model name is STALE, fix advisor.lane_models to a real "
    "id. Be specific to THIS error; do not invent a project id or account you were not given.")

_REMEDIATE_SCHEMA = {"type": "object", "properties": {
    "issue": {"type": "string"}, "fix": {"type": "string"}, "command": {"type": "string"}},
    "required": ["issue", "fix"]}


def _remediation_db():
    from . import budget
    db = budget._ledger_db()
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS lane_remediation "
                   "(signature TEXT PRIMARY KEY, kind TEXT, resource TEXT, issue TEXT, fix TEXT, command TEXT, ts TEXT)")
        db.commit()
    return db


def _remediation_signature(kind, resource, reason):
    """Cache key = the EXACT (kind, resource, error) hashed — mechanical IDENTITY, never a 'same failure class'
    judgement (deciding two different error strings mean the same thing is the LLM's call, not a regex's — and
    guessing wrong would serve a rate-limit fix for a billing failure). Two errors share a cached remediation ONLY
    when byte-identical; any change re-classifies (safe). The core lane failures (OAuth expired, credits depleted,
    API not enabled) are STABLE strings, so a routine check still avoids re-paying for a persistent problem."""
    return hashlib.sha256(f"{kind}|{resource}|{reason}".encode("utf-8", "replace")).hexdigest()[:24]


def _classify_remediation(kind, resource, reason, model=None):
    """The fix for ONE unreachable resource — CACHED by signature (no re-pay for a known failure class), decided
    AGENTICALLY (what an error means + how to fix it is a judgement). The WHOLE error is sent (never truncated).
    Fail-open: on any non-deliberate failure, a generic remediation (never breaks the health check); a DELIBERATE
    stop propagates."""
    from . import budget, adapters, calls, config, gate
    sig = _remediation_signature(kind, resource, reason)
    db = _remediation_db()
    with budget._lock:
        row = db.execute("SELECT issue,fix,command FROM lane_remediation WHERE signature=?", (sig,)).fetchone()
    if row:
        return {"issue": row[0], "fix": row[1], "command": row[2] or "", "cached": True}
    model = model or config.advisor_model()
    prompt = f"resource: {kind} {resource}\nerror: {reason}\n\nGive the user the fix."   # WHOLE error, never cut
    try:
        with calls.context(intent="spendguard:lane-remediation"):
            # No max_tokens literal — the sig sizes the OUTPUT budget from this call-class's MEASURED p99 (the small
            # issue/fix/command JSON sits well under the floor), so there is no hand-picked cap to justify.
            r = adapters.call(model, prompt, system=_REMEDIATE_SYS,
                              schema=_REMEDIATE_SCHEMA, sig="spendguard:lane-remediation")
    except Exception as e:
        if gate.is_deliberate_stop(e):
            raise
        return {"issue": str(reason)[:120], "fix": "inspect the lane's auth/quota/model config", "command": "", "cached": False}
    j = r.get("json")
    if not isinstance(j, dict):
        import json as _json
        try:
            j = _json.loads(r.get("text") or "")
        except Exception:
            j = None
    if not isinstance(j, dict) or not j.get("issue"):
        return {"issue": str(reason)[:120], "fix": "inspect the lane's auth/quota/model config", "command": "", "cached": False}
    out = {"issue": str(j.get("issue"))[:200], "fix": str(j.get("fix"))[:200], "command": str(j.get("command") or "")[:200]}
    import datetime
    with budget._lock:                                    # cache a REAL verdict so the next routine check is $0 for it
        db.execute("INSERT OR REPLACE INTO lane_remediation VALUES (?,?,?,?,?,?,?)",
                   (sig, kind, resource, out["issue"], out["fix"], out["command"],
                    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
        db.commit()
    return {**out, "cached": False}


def remediate(sweep_result, model=None):
    """Per UNREACHABLE lane/provider in a sweep result, the agentic remediation (cached). [] when all healthy —
    so a routine check is $0 when nothing is wrong, and only classifies NEW failures. This is the 'tell me which
    lane needs a login' layer: schedule `spendguard reliability --run --remediate` and act on its ACTION lines."""
    down = []
    for lane, d in (sweep_result.get("lanes") or {}).items():
        if not d.get("reachable"):
            down.append(("lane", lane, d.get("reason")))
    for prov, d in (sweep_result.get("metered") or {}).items():
        if not d.get("reachable"):
            down.append(("metered", prov, d.get("reason")))
    return [{"kind": kind, "resource": name, "reason": reason, **_classify_remediation(kind, name, reason, model)}
            for kind, name, reason in down]


def main(argv=None):
    argv = list(argv or [])
    run = "--run" in argv
    if "--json" in argv:                                 # machine-readable status of every lane + metered provider
        import json as _json
        print(_json.dumps(sweep(run=run), indent=2))
        return 0
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
    if "--remediate" in argv:                            # tell the user EXACTLY what to fix for each down resource
        acts = remediate(res)                            # agentic + cached — $0 when all healthy, only pays for NEW failures
        if not acts:
            print("\n  ✅ ALL HEALTHY — no action needed.")
        else:
            print(f"\n  🔧 ACTION NEEDED ({len(acts)}):")
            for a in acts:
                print(f"    [{a['kind']}] {a['resource']}: {a.get('issue')}")
                print(f"        → {a.get('fix')}" + (f"    ·    run: {a['command']}" if a.get("command") else ""))
    return 0
