"""MEASURED lane↔model servability — which models a subscription LANE actually serves right now, learned from the
ledger's own outcomes, and a SHIFT detector that fires loud when a model a lane used to serve stops serving.

Why this exists (measured 2026-10-06): codex-cli 0.160.1 dropped `gpt-5.6-sol` from the ChatGPT plan while the
METERED OpenAI API kept listing it. A `/models` cross-check alone could NOT catch that — the metered endpoint has no
view of what a *plan* serves. So the authoritative signal for a lane/plan drop is the lane's OWN measured outcomes
(calls.lane_model_outcomes): a (lane, model) that was succeeding and flips to a wall of transport_errors with ZERO
successes is a drop, established by the PATTERN, never by parsing an error string (agentic-decisions doctrine: this is
measurement, not prose). On a detected shift this module ALSO re-pulls the provider's live /models ($0) to tell a
lane/plan drop (id still metered-listed) from a provider-wide drop (id gone) from a rename (a near-match appeared),
writes the result into the catalog's lane_models/lane_unserved namespaces, and raises LOUD.

Nothing here decides meaning with regex; thresholds are counts over MEASURED outcomes and are config-overridable, never
hardcoded. $0 (ledger reads + a free /models GET). Never raises on the hot path — a servability read must not break
routing."""
import os
import sys

from . import config

# Policy thresholds — DEFAULTS here, each overridable via config [lane_servability] or the matching env var, so a
# different estate tunes them without editing source (no-hardcode rule). They are counts over MEASURED outcomes.
_WINDOW_HOURS_DEFAULT = 48        # the "recent" window the served/rejected verdict is measured over
_MIN_FAIL_DEFAULT = 12            # recent failures with zero recent successes before a model is called rejected
_MIN_PRIOR_OK_DEFAULT = 50        # all-time successes that make a fresh wall of failures a SHIFT (vs never-worked)
_SHIFT_FAIL_DEFAULT = 20          # recent failures that trip the LOUD shift alert + /models re-pull (the operator's 10–20)
_OK_RATE_MIN_TOTAL_DEFAULT = 20   # recent attempts required before a success RATE is trusted (evidence sufficiency, not a quality bound)


def _int_cfg(key, default, env):
    """A single integer policy knob: env var wins, then config [lane_servability].<key>, then the default. Any
    unparseable value falls back to the default rather than raising on the routing path."""
    try:
        v = os.environ.get(env)
        if v is None:
            v = config._cfg_get("lane_servability", key, None)
        return int(v) if v is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _opt_float_cfg(key, env):
    """An OPTIONAL float policy knob — env var wins, then config [lane_servability].<key>, else None. Returns None
    when the operator has set no value: there is deliberately NO baked-in default, because this knob is a quality
    bound (what success rate is 'acceptable') that only the operator should pick — code must not hand-pick it."""
    try:
        v = os.environ.get(env)
        if v is None:
            v = config._cfg_get("lane_servability", key, None)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _window_hours():
    return _int_cfg("window_hours", _WINDOW_HOURS_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_WINDOW_HOURS")


def lane_readiness(lane):
    """Readiness from MEASURED recent outcomes, NOT just auth/binary presence — defect 2 (codex printed '🟢 ready'
    while failing 4 of 5 calls, because the probe only checked that a binary + token existed). Returns
    {"ok_rate": float|None, "ok": int, "total": int, "rejected": [...], "shifted": [...], "degraded": bool}.
    ok_rate is None when there is no recent evidence (never read as 'bad'). `rejected`/`shifted` name the SPECIFIC
    dropped models (a zero-recent-success + sustained-failure PATTERN — measurement, not a hand-picked bound), so a
    surface says WHICH model died rather than condemning a lane that still serves its others (codex still serves luna
    + gpt-6-sol). `degraded` is a broad-failure flag that trips ONLY when the operator has explicitly CONFIGURED a
    success-rate floor ([lane_servability].ok_rate_floor) and it is breached with enough attempts to trust — there is
    no code-chosen cutoff, because 'what rate is acceptable' is the operator's judgement. $0 ledger read; never raises."""
    obs = observed_lane_models(lane)
    ok = sum(int(d.get("recent_ok", 0)) for d in obs.get("detail", {}).values())
    fail = sum(int(d.get("recent_fail", 0)) for d in obs.get("detail", {}).values())
    total = ok + fail
    rate = (ok / total) if total else None
    floor = _opt_float_cfg("ok_rate_floor", "SPENDGUARD_LANE_SERVABILITY_OK_RATE_FLOOR")
    min_total = _int_cfg("ok_rate_min_total", _OK_RATE_MIN_TOTAL_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_OK_RATE_MIN_TOTAL")
    degraded = bool(floor is not None and rate is not None and total >= min_total and rate < floor)
    return {"ok_rate": rate, "ok": ok, "total": total,
            "rejected": obs.get("rejected", []), "shifted": obs.get("shifted", []), "degraded": degraded}


def _since_iso(hours):
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=int(hours))).isoformat()


def _lanes():
    """The configured subscription lanes and each one's metered PROVIDER (for a /models cross-check), derived from
    adapters._LANES ({provider: (lane, model)}) — never a hardcoded list."""
    try:
        from . import adapters
        return {lane: prov for prov, (lane, _m) in adapters._LANES.items()}
    except Exception:
        return {}


def observed_lane_models(lane, window_hours=None):
    """The MEASURED verdict for one lane: {"served": [...], "rejected": [...], "shifted": [...], "asof": iso,
    "detail": {model: {...}}}. A model is SERVED if it has ≥1 success in the recent window; REJECTED if it has zero
    recent successes and ≥min_fail recent failures; SHIFTED (a subset of rejected, the loud case) if it ALSO has
    ≥min_prior_ok successes across all history — i.e. it used to work and stopped. Pure read; writes nothing. {}-ish
    safe default on any error."""
    out = {"served": [], "rejected": [], "shifted": [], "asof": None, "detail": {}}
    if not lane:
        return out
    try:
        from . import calls
        wh = int(window_hours if window_hours is not None else _window_hours())
        min_fail = _int_cfg("min_fail", _MIN_FAIL_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_MIN_FAIL")
        min_prior_ok = _int_cfg("min_prior_ok", _MIN_PRIOR_OK_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_MIN_PRIOR_OK")
        recent = calls.lane_model_outcomes(lane, since=_since_iso(wh))
        alltime = calls.lane_model_outcomes(lane, since=None)
        import datetime as _dt
        out["asof"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        for model in sorted(set(recent) | set(alltime)):
            r = recent.get(model, {"ok": 0, "fail": 0})
            a = alltime.get(model, {"ok": 0, "fail": 0})
            rec = {"recent_ok": r.get("ok", 0), "recent_fail": r.get("fail", 0),
                   "total_ok": a.get("ok", 0), "last_ok": r.get("last_ok") or a.get("last_ok"),
                   "last_fail": r.get("last_fail") or a.get("last_fail")}
            out["detail"][model] = rec
            if rec["recent_ok"] > 0:
                out["served"].append(model)
            elif rec["recent_fail"] >= min_fail:
                out["rejected"].append(model)
                if rec["total_ok"] >= min_prior_ok:
                    rec["shift"] = True
                    out["shifted"].append(model)
            # else: too little evidence either way — neither served nor rejected (never a guess)
    except Exception:
        pass
    return out


def _provider_models_now(provider):
    """The provider's live METERED /models ids right now — a free GET via the catalog's own fetch primitive. None on
    any failure (cannot-check, never read as 'gone')."""
    try:
        from . import vendor_call
        res = vendor_call.list_models(provider)
        if res.get("error"):
            return None
        return sorted({m.get("id") for m in (res.get("models") or []) if m.get("id")})
    except Exception:
        return None


def _classify_drop(provider, model, metered_ids):
    """Tell WHY a lane stopped serving a model, from the fresh metered list: 'provider-drop' (gone from metered too),
    'lane-drop' (still metered, so the PLAN dropped it — the 10-06 case), or 'rename' with the near-match. metered_ids
    None → 'unknown' (the cross-check itself could not run). Near-match is the vendor's own closest_served (identity),
    not a meaning judgement."""
    if metered_ids is None:
        return {"kind": "unknown", "replacement": None}
    if model not in set(metered_ids):
        try:
            from . import vendor_call
            near = vendor_call.closest_served(provider, model)
        except Exception:
            near = None
        if near and near != model:
            return {"kind": "rename", "replacement": near}
        return {"kind": "provider-drop", "replacement": None}
    return {"kind": "lane-drop", "replacement": None}


def detect_served_shift(lane, announce=True):
    """Detect models this lane USED TO serve that have shifted to a wall of transport errors, and for each: re-pull the
    provider's live /models ($0) to classify the drop (lane/plan vs provider-wide vs rename), raise LOUD, and return a
    record. The loud-raise trigger is ≥shift_fail recent failures (the operator's 10–20). Returns [] when nothing
    shifted. Does NOT itself swap a model — resolution is lane_served_substitute's job on the next dispatch; this names
    the problem and feeds the catalog so that resolution has ground to stand on."""
    shifts = []
    try:
        shift_fail = _int_cfg("shift_fail", _SHIFT_FAIL_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_SHIFT_FAIL")
        obs = observed_lane_models(lane)
        provider = _lanes().get(lane)
        metered = None
        for model in obs.get("shifted", []):
            rec = obs["detail"].get(model, {})
            if rec.get("recent_fail", 0) < shift_fail:
                continue                                   # a real shift but below the loud-alert floor — recorded by refresh, not shouted
            if metered is None and provider:
                metered = _provider_models_now(provider)   # pull once per lane, only when a loud shift is present
            why = _classify_drop(provider, model, metered)
            shift = {"lane": lane, "model": model, "provider": provider,
                     "recent_fail": rec.get("recent_fail", 0), "total_ok": rec.get("total_ok", 0), **why}
            shifts.append(shift)
            if announce:
                _announce_shift(shift)
    except Exception:
        pass
    return shifts


def _announce_shift(shift):
    """Raise a shift LOUD — a persistent reliability alert (rides the receipt banner) plus a one-line stderr notice
    with the actionable diagnosis. Never raises."""
    lane, model = shift.get("lane"), shift.get("model")
    kind, repl = shift.get("kind"), shift.get("replacement")
    if kind == "lane-drop":
        diag = (f"the {lane} plan dropped {model!r} (still on the metered API) — route this lane's work to a served "
                f"model (`spendguard lanes set-model {lane} <tier> <served-id>`)")
    elif kind == "provider-drop":
        diag = f"{model!r} is gone from the provider entirely — pick a current model"
    elif kind == "rename":
        diag = f"{model!r} looks renamed to {repl!r} — update the config / lane_models to the new id"
    else:
        diag = f"{model!r} stopped serving on {lane}; the /models cross-check could not run (check the key/network)"
    msg = (f"lane {lane}: {shift.get('recent_fail')} recent transport failures on {model!r} after "
           f"{shift.get('total_ok')} prior successes — {diag}")
    try:
        from . import reliability
        reliability.note_lane_model_shift(lane, model, msg)
    except Exception:
        pass
    print(f"[spendguard] ⚠ LANE SERVABILITY SHIFT — {msg}", file=sys.stderr)


def lane_served_substitute(lane, requested):
    """(served_id, reason) — resolve a model the LANE is MEASURED to no longer serve to one it DOES serve, or
    (requested, None) when `requested` is not lane-rejected (nothing to do) or no served replacement exists.

    The lane-aware sibling of vendor_call.served_substitute: THAT one resolves against the vendor's metered∪lane
    UNION, so it leaves gpt-5.6-sol alone (the metered OpenAI API still serves it) and never sees the plan drop;
    THIS keys on the LANE's own measured reject/serve sets, the only place a plan drop is visible. The pick is
    DETERMINISTIC and $0 — the operator's own declared lane models (advisor.lane_models per tier), filtered to ones
    the lane is measured to still serve, STRONG first so a dropped model's replacement never silently DOWNGRADES
    capability; then any other served id as a last resort. No LLM call and no error-prose parsing. A lane
    substitution is a DIFFERENT model, so the caller must NOT apply it under no_substitution (measurement) — that
    gate is the caller's; this function is only ever invoked on the non-pinned path."""
    if not lane or not requested:
        return requested, None
    try:
        from . import catalog, lane_catalog
        rejected = set(catalog.lane_unserved_ids(lane) or [])
        if requested not in rejected:
            return requested, None                        # not known-dead on this lane → nothing to resolve
        served = set(catalog.lane_model_ids(lane) or [])
        candidates = []
        for tier in ("strong", "cheap"):                  # operator intent, strong-first (no capability downgrade)
            m = lane_catalog.lane_model_for_tier(lane, tier)
            if m and m in served and m not in rejected and m not in candidates:
                candidates.append(m)
        for m in sorted(served - rejected):               # last resort: any other measured-served id
            if m not in candidates:
                candidates.append(m)
        if not candidates:
            return requested, None                        # nothing served to fall to → honest metered path upstream
        pick = candidates[0]
        return pick, f"{lane} no longer serves {requested} → its served model {pick}"
    except Exception:
        return requested, None


_REFRESH_THROTTLE_S_DEFAULT = 600          # process-local: at most one failure-triggered refresh per lane per window
_last_refresh = {}                         # lane -> monotonic ts of the last failure-triggered refresh


def maybe_refresh_on_failure(lane):
    """Throttled trigger for the dispatch FAILURE path: when a lane call misses, re-measure that lane's servability
    (cheap ledger read; the /models re-pull fires only if a real shift is present) so a NEW plan drop is recorded and
    shouted within one throttle window instead of bleeding to metered all day. Process-local throttle (the ledger it
    reads is shared, so one refresh per process per window is enough). $0 on the common path. Never raises — it rides
    the error path and must not turn a recoverable miss into a crash."""
    if not lane:
        return
    try:
        import time as _t
        interval = _int_cfg("refresh_throttle_s", _REFRESH_THROTTLE_S_DEFAULT, "SPENDGUARD_LANE_SERVABILITY_REFRESH_S")
        now = _t.monotonic()
        last = _last_refresh.get(lane)
        if last is not None and (now - last) < interval:
            return
        _last_refresh[lane] = now
        refresh_lane_servability(lane)
    except Exception:
        pass


def refresh_lane_servability(lane=None, announce=True):
    """Observe every configured lane (or one), write its served/rejected sets into the catalog, and fire the loud
    shift detector. The $0 entry point a periodic refresh or a transport-error burst calls. Returns
    {lane: {"served", "rejected", "shifted", "shifts"}}. Never raises."""
    out = {}
    try:
        from . import catalog
        lanes = [lane] if lane else list(_lanes().keys())
        for ln in lanes:
            obs = observed_lane_models(ln)
            if obs["served"] or obs["rejected"]:
                catalog.merge_lane_observation(ln, obs["served"], obs["rejected"], asof=obs["asof"])
            shifts = detect_served_shift(ln, announce=announce) if obs["shifted"] else []
            out[ln] = {"served": obs["served"], "rejected": obs["rejected"],
                       "shifted": obs["shifted"], "shifts": shifts}
    except Exception:
        pass
    return out
