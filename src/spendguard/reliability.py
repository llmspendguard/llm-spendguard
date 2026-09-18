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
import json

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
    "or not logged in → re-login (claude-code: `claude auth login` then verify `claude auth status` shows "
    "loggedIn:true — NOT setup-token, which does not persist for the headless lane; codex: `codex login`; "
    "gemini/agy: re-auth the agy CLI); "
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
    j = adapters.structured_reply(r)
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


_EVENT_NOTIFY_THROTTLE_S = 1800          # per-lane: notify on a mid-use failure at most once per 30 min (no spam)


def _health_db():
    from . import budget
    db = budget._ledger_db()
    with budget._lock:
        db.execute("CREATE TABLE IF NOT EXISTS lane_health "
                   "(resource TEXT PRIMARY KEY, kind TEXT, reachable INTEGER, reason TEXT, fix TEXT, command TEXT, "
                   "ts TEXT, source TEXT, notified_ts TEXT)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(lane_health)")]
        for c in ("source", "notified_ts"):              # migrate a table created before event-driven health
            if c not in cols:
                db.execute(f"ALTER TABLE lane_health ADD COLUMN {c} TEXT")
        db.commit()
    return db


def note_lane_down(lane, reason):
    """EVENT-driven health: a lane's call AND its metered-API fallback both missed ONE call ('down'). A LONE down is a
    SUSPECTED blip — under load a lane misses a call, COOLS, is SKIPPED for its cooldown, and recovers on the next
    retry — so alarming on it is a false toast (the flapping seen under heavy fans). We RECORD it (the receipt
    surfaces it), but ALARM only on a SUSTAINED down: a 'down' while the lane is ALREADY an UNRESOLVED event-down
    (reachable=0, source 'event'), i.e. it failed AGAIN before ever serving successfully. Resolution is a real SUCCESS
    (note_lane_ok, called from adapters on a served call) or an authoritative sweep — never a clock, so no time
    threshold decides 'sustained'; the state lives in the SHARED lane_health row, so it is correct ACROSS processes.
    Throttled. $0, no LLM. NEVER raises — on the call path."""
    try:
        import datetime
        from . import budget
        now = datetime.datetime.now(datetime.timezone.utc)
        db = _health_db()
        with budget._lock:
            prev = db.execute("SELECT reachable, source, notified_ts, fix, command FROM lane_health WHERE resource=?",
                              (lane,)).fetchone()
        # SUSTAINED = the row is ALREADY an unresolved event-down (a real success or a sweep would have cleared it) →
        # the lane failed again without recovering. A pure STATE read of the shared row, not a magnitude/time cutoff.
        sustained = bool(prev and prev[0] == 0 and (prev[1] or "").startswith("event"))
        fix, cmd = (prev[3], prev[4]) if prev else (None, None)
        try:                                             # refresh a cached remediation for THIS exact failure ($0); never
            with budget._lock:                           # clobber a known fix/command with an empty lookup — preserve it
                rr = db.execute("SELECT fix,command FROM lane_remediation WHERE signature=?",
                                (_remediation_signature("lane", lane, reason),)).fetchone()
            if rr and rr[0] is not None:
                fix, cmd = rr[0], rr[1]
        except Exception:
            pass
        with budget._lock:                               # record the down: the receipt surfaces it; note_lane_ok/sweep clears it
            db.execute("INSERT OR REPLACE INTO lane_health "
                       "(resource,kind,reachable,reason,fix,command,ts,source,notified_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                       (lane, "lane", 0, reason, fix, cmd, now.isoformat(timespec="seconds"), "event",
                        prev[2] if prev else None))
            db.commit()
        if not sustained:
            return                                       # a LONE down = a suspected blip → recorded, NOT alarmed
        last = None
        if prev and prev[2]:
            try:
                last = datetime.datetime.fromisoformat(prev[2])
            except Exception:
                last = None
        if last is None or (now - last).total_seconds() >= _EVENT_NOTIFY_THROTTLE_S:
            _notify_macos("spendguard: lane %s down" % lane,
                          (fix or "run: spendguard reliability --run --remediate"))
            with budget._lock:
                db.execute("UPDATE lane_health SET notified_ts=? WHERE resource=?",
                           (now.isoformat(timespec="seconds"), lane))
                db.commit()
    except Exception:
        pass


def note_lane_ok(lane):
    """RECOVERY signal: a lane just SERVED a call successfully (adapters._lane_note_ok) → any outage is over. Clear an
    UNRESOLVED event-down row (reachable=1) so the receipt stops surfacing it AND the NEXT down is read as a fresh
    blip, not 'sustained'. A single conditional UPDATE keyed on the shared row — cross-process correct, and a no-op
    for a healthy lane (the WHERE matches nothing). Never touches a sweep's authoritative row. NEVER raises."""
    try:
        import datetime
        from . import budget
        db = _health_db()
        with budget._lock:
            db.execute("UPDATE lane_health SET reachable=1, reason=NULL, source='event-recovered', ts=? "
                       "WHERE resource=? AND reachable=0 AND source LIKE 'event%'",
                       (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), lane))
            db.commit()
    except Exception:
        pass


def _persist_health(sweep_result, acts=None):
    """Record the last check's reachability (+ any remediation fixes) so the RECEIPT and the notifier can read it
    with NO new sweep — that is what lets a red lane surface in every conversation for $0. One row per resource.

    A sweep is AUTHORITATIVE for REACHABILITY (source=sweep, clears an event row too), but it must NOT clobber a
    cached remediation fix/command (paid, from a prior --remediate) with NULL just because THIS plain sweep carried
    no acts: a resource that is STILL unreachable keeps its known fix until a fresh remediation replaces it; a
    resource that RECOVERED clears its fix (a healthy lane has no fix). So fix loss is impossible from a bare sweep."""
    import datetime
    from . import budget
    fixes = {(a["kind"], a["resource"]): a for a in (acts or [])}
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    db = _health_db()
    with budget._lock:
        prior = {r[0]: (r[1], r[2]) for r in db.execute("SELECT resource, fix, command FROM lane_health").fetchall()}

    def _health_row(res, kind, d):
        reachable = bool(d.get("reachable"))
        if reachable:
            fix = cmd = None                             # healthy → clear any stale fix (a reachable lane needs none)
        else:
            f = fixes.get((kind, res), {})
            _pf, _pc = prior.get(res, (None, None))
            fix = f.get("fix") if f.get("fix") is not None else _pf      # still-down → keep a KNOWN fix, never NULL it
            cmd = f.get("command") if f.get("command") is not None else _pc
        return (res, kind, 1 if reachable else 0, d.get("reason"), fix, cmd, ts, "sweep", None)

    rows = [_health_row(lane, "lane", d) for lane, d in (sweep_result.get("lanes") or {}).items()]
    rows += [_health_row(prov, "metered", d) for prov, d in (sweep_result.get("metered") or {}).items()]
    with budget._lock:
        db.executemany("INSERT OR REPLACE INTO lane_health "
                       "(resource,kind,reachable,reason,fix,command,ts,source,notified_ts) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        db.commit()


def health_reds(since_hours=48):
    """The resources recorded UNREACHABLE by the last check (fresh within since_hours) → [{resource,kind,fix,command}].
    $0 — a cached read, safe to call from the receipt on every turn. [] when all healthy or no recent check."""
    import datetime
    from . import budget
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=since_hours)).isoformat(timespec="seconds")
    try:
        db = _health_db()
        with budget._lock:
            rows = db.execute("SELECT resource,kind,fix,command,source FROM lane_health WHERE reachable=0 AND ts>=?",
                              (cutoff,)).fetchall()
        out, recovered = [], []
        for resource, kind, fix, command, source in rows:
            if source == "event" and kind == "lane":     # an EVENT down is stale once the lane stops cooling: it
                try:                                      # RECOVERED. Resolve it (below) rather than silently drop it.
                    from . import adapters
                    if not adapters._lane_cooling(resource):
                        recovered.append(resource)
                        continue
                except Exception:
                    pass
            out.append({"resource": resource, "kind": kind, "fix": fix, "command": command})
        if recovered:                                    # SELF-HEAL, traced (a DB write, not a silent skip): a recovered
            with budget._lock:                            # event-lane is marked reachable so it clears everywhere at once
                db.executemany("UPDATE lane_health SET reachable=1 WHERE resource=? AND source='event'",
                               [(r,) for r in recovered])
                db.commit()
        return out
    except Exception:
        return []                                        # a health read must NEVER break the receipt


def health_alert():
    """A ONE-LINE receipt alert when a lane/provider is down (from the last check), or None. Rides the receipt that
    already prints every turn, so 'which login do I fix' reaches every conversation without a new sweep. $0."""
    reds = health_reds()
    if not reds:
        return None
    tip = next((r for r in reds if r.get("fix")), None)
    head = "⚠ spendguard: %d resource(s) unreachable (%s)" % (
        len(reds), ", ".join(r["resource"] for r in reds[:4]) + ("…" if len(reds) > 4 else ""))
    if tip and tip.get("fix"):
        head += " — %s: %s" % (tip["resource"], tip["fix"]) + (" [%s]" % tip["command"] if tip.get("command") else "")
    else:
        head += " — run `spendguard reliability --run --remediate` for the fix"
    return head


def _notify_macos(title, message):
    """Best-effort macOS notification (osascript). Silent no-op off macOS or if osascript is absent — a notifier
    must never break the check."""
    try:
        import subprocess
        subprocess.run(["osascript", "-e",
                        'display notification %s with title %s' % (json.dumps(message), json.dumps(title))],
                       capture_output=True, timeout=10)
    except Exception:
        pass


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
    acts = None
    if "--remediate" in argv:                            # tell the user EXACTLY what to fix for each down resource
        acts = remediate(res)                            # agentic + cached — $0 when all healthy, only pays for NEW failures
        if not acts:
            print("\n  ✅ ALL HEALTHY — no action needed.")
        else:
            print(f"\n  🔧 ACTION NEEDED ({len(acts)}):")
            for a in acts:
                print(f"    [{a['kind']}] {a['resource']}: {a.get('issue')}")
                print(f"        → {a.get('fix')}" + (f"    ·    run: {a['command']}" if a.get("command") else ""))
    _persist_health(res, acts)                           # cache the result so the RECEIPT + notifier read it for $0 later
    if "--notify" in argv:                               # macOS push on any red (for the SCHEDULED, headless run)
        reds = health_reds()
        if reds:
            _notify_macos("spendguard: %d lane/API down" % len(reds),
                          (reds[0].get("fix") or "run: spendguard reliability --run --remediate")
                          + " — " + ", ".join(r["resource"] for r in reds[:5]))
    return 0
