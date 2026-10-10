"""4b — the OTHER admission door. bulk_delegate/submit_storm are the EXPLICIT fan entries; the actual 429-storm caller
was the IMPLICIT one: honestreview's own ThreadPoolExecutor firing many independent adapters.call(metered_only) at one
vendor. 4b routes that raw concurrent traffic through ONE shared StormCoalescer per identical call-shape, so the
implicit fan gets the same pace+batch COMBO as the explicit entries — pacing the sustainable realtime share under the
(xp_rate-governed) wall and diverting the overflow to the Batch API.

DEFAULT ON (kill switch SPENDGUARD_STORM_COALESCE=0). Safe on because it routes ONLY the raw implicit fan: the
explicit fans (bulk_delegate/submit_storm) pass adapters.call(_coalesce_eligible=False) — they are already governed by
the dispatch connection-window AND have native Batch-API offload, so re-coalescing them would replace a proven ~1s
realtime path with a batch divert (minutes), strictly worse (see coalescing_enabled). The registry is bounded by
idle+cap eviction (_evict_idle) so default-on cannot grow one persistent planner thread per shape forever.

Keyed by the FULL call shape (vendor, model, intent, reasoning, system): a shared coalescer bakes system+reasoning
into its realtime + batch executors, so only calls that are identical on those may coalesce (the incident fan is —
same intent/model/reasoning/system); any difference gets its own coalescer (correctness over maximal coalescing). The
realtime executor re-enters adapters.call with _route=False AND _coalesce_eligible=False (two independent guards) — so
a routed call still rides the governed metered path (dispatch.admit → the cross-process rate window → _call_guarded). A
lone call hits quiescence and resolves fast, so routing is safe for non-storm traffic too; route() is best-effort and
falls back to the direct path on any setup failure, so it can never make a call worse than not routing.
"""
import hashlib
import os
import threading
import time

from . import adapters, dispatch
from .storm_coalescer import StormCoalescer
from .storm_submit import default_batch_executor, governed_realtime_executor

_REG = {}
_LAST_USED = {}                 # key -> monotonic ts of the last get_coalescer; drives idle eviction (default-on bound)
_REG_LOCK = threading.Lock()
_DEFAULT_HORIZON_S = 30.0
_DEFAULT_IDLE_TTL_S = 120.0     # a coalescer idle (no queue, no in-flight) this long is reaped — its planner thread exits
_DEFAULT_REG_MAX = 256          # hard cap on live coalescers; over it, least-recently-used IDLE shapes are reaped too


def coalescing_enabled():
    """4b routing is ON by default (kill switch SPENDGUARD_STORM_COALESCE=0). It is safe on because it routes ONLY the
    RAW implicit fan: the explicit fans (bulk_delegate/submit_storm) pass _coalesce_eligible=False and so are never
    coalesced — they are ALREADY governed against the storm by the dispatch connection-window (the AIMD learn→ratchet
    that test_connection_storm_reliability proves absorbs the burst in realtime, ~1s) AND carry their own native
    Batch-API offload. Re-coalescing them would replace a proven ~1s realtime path with a batch divert (minutes).
    So 4b is the net under raw adapters.call concurrency only, where no explicit governor sits in front."""
    return os.environ.get("SPENDGUARD_STORM_COALESCE", "1") != "0"


def _idle_ttl_s():
    try:
        v = float(os.environ.get("SPENDGUARD_STORM_IDLE_TTL_S") or _DEFAULT_IDLE_TTL_S)
        return v if v > 0 else _DEFAULT_IDLE_TTL_S
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_TTL_S


def _reg_max():
    try:
        v = int(os.environ.get("SPENDGUARD_STORM_REG_MAX") or _DEFAULT_REG_MAX)
        return v if v > 0 else _DEFAULT_REG_MAX
    except (TypeError, ValueError):
        return _DEFAULT_REG_MAX


def _evict_idle(now):
    """Reap coalescers whose shape has gone quiet, so default-on cannot grow one persistent planner thread per shape
    forever. Two passes under _REG_LOCK: (1) TTL — any coalescer idle longer than the idle TTL; (2) CAP — if still over
    _reg_max, the least-recently-used IDLE coalescers until at/under the cap. Only is_idle() coalescers are ever closed,
    so eviction NEVER abandons an in-flight cohort (a busy shape is kept regardless of age). close(wait=False) is called
    OUTSIDE the lock (it joins the planner, which exits at once for an idle coalescer). Returns the count reaped."""
    ttl = _idle_ttl_s()
    cap = _reg_max()
    doomed = []
    over_cap = 0
    with _REG_LOCK:
        for key in list(_REG.keys()):
            if now - _LAST_USED.get(key, now) > ttl and _REG[key].is_idle():
                doomed.append(_REG.pop(key))
                _LAST_USED.pop(key, None)
        if len(_REG) > cap:
            for key in sorted(_REG.keys(), key=lambda k: _LAST_USED.get(k, 0.0)):
                if len(_REG) <= cap:
                    break
                if _REG[key].is_idle():
                    doomed.append(_REG.pop(key))
                    _LAST_USED.pop(key, None)
            over_cap = len(_REG) - cap            # still over cap ⇒ every remaining excess coalescer is BUSY (not reapable)
    for c in doomed:
        try:
            c.close(wait=False)
        except Exception:
            pass
    if over_cap > 0:
        # SURFACE it — the cap could NOT be enforced this pass because the excess coalescers are all mid-cohort, and a
        # silently-exceeded bound is exactly the kind of invisible drift the project forbids. The bound re-applies as
        # they go idle (close() reaps them on a later pass); a persistent warning means a workload legitimately needs
        # more concurrent call-shapes than the cap allows.
        import sys as _sys
        print("[spendguard] storm_route: registry over cap by %d — %d live coalescers vs SPENDGUARD_STORM_REG_MAX=%d; "
              "the excess are all BUSY (active cohorts) so none could be reaped now. Raise SPENDGUARD_STORM_REG_MAX if "
              "this persists." % (over_cap, cap + over_cap, cap), file=_sys.stderr)
    return len(doomed)


def _horizon_s():
    """The urgency horizon that sizes each coalescer's realtime budget (rate × horizon). Overridable via
    SPENDGUARD_STORM_HORIZON_S; defaults to _DEFAULT_HORIZON_S."""
    try:
        v = float(os.environ.get("SPENDGUARD_STORM_HORIZON_S") or _DEFAULT_HORIZON_S)
        return v if v > 0 else _DEFAULT_HORIZON_S
    except (TypeError, ValueError):
        return _DEFAULT_HORIZON_S


def _coalescer_key(vendor, model, intent, reasoning, system):
    sysh = hashlib.sha1((system or "").encode("utf-8")).hexdigest()[:12]   # a stable, compact system fingerprint
    return (vendor, model, intent or "", reasoning or "", sysh)


def get_coalescer(vendor, model, intent, system=None, reasoning="minimal", horizon_s=_DEFAULT_HORIZON_S,
                  clock=time.monotonic):
    """The shared StormCoalescer for one exact call shape (created lazily). Returns None when no rpm is known for the
    vendor (dispatch.rate_per_s is None → nothing to pace against → the caller runs its normal direct path). Each call
    reaps idle coalescers first and stamps this shape's last-used time BEFORE returning, so a concurrent eviction can
    never close the coalescer we are about to hand back (its timestamp is fresh)."""
    rate = dispatch.rate_per_s(vendor, model)      # the ONE rpm/60 resolver; None policy = decline to coalesce
    if rate is None:
        return None
    key = _coalescer_key(vendor, model, intent, reasoning, system)
    now = clock()
    _evict_idle(now)                               # bound default-on: reap shapes gone quiet / over the cap (idle only)
    with _REG_LOCK:
        c = _REG.get(key)
        if c is None:
            c = StormCoalescer(
                # The urgency HORIZON sizes realtime CAPACITY (rate × horizon) — it is NOT a per-call reply deadline.
                # Passing it as the executor's timeout_s killed any realtime reply that legitimately took longer than the
                # horizon (default 30s), which then rerouted to batch and came back text=None — the measured "coalescer
                # chokes on LARGE replies → NO REPLY" (2026-10-10). Pass None so the executor's adapters.call derives the
                # real per-prompt deadline from deadline_for (output-budget-aware: it sizes UP for a large expected
                # reply). Horizon stays below for capacity sizing only.
                execute_realtime=governed_realtime_executor(model, system, reasoning, intent, None),
                execute_batch=default_batch_executor(model, intent, system=system, reasoning=reasoning),
                sustainable_rate_per_s=rate, horizon_s=horizon_s, provider=vendor)
            _REG[key] = c
        _LAST_USED[key] = now                      # fresh stamp → protects it from the next _evict_idle pass
        return c


def route(model, prompt, intent, system=None, reasoning="minimal"):
    """Submit one labelled call into the shared coalescer for its exact shape and BLOCK on the result. Returns the
    result dict (success OR a coalescer error result — authoritative once the request entered the coalescer, so the
    caller never re-runs it and cannot double-spend), or None when routing is NOT ATTEMPTED — disabled, no known rate,
    or anything about SETTING UP the route fails. NEVER raises: adapters.call's contract is 'never raises', and routing
    is best-effort on top of it, so any pre-submit failure (a malformed model id → provider_for raising, a closed-
    coalescer race → submit_request raising) falls back to None and the caller runs its normal DIRECT governed path
    (itself fully paced by the xp_rate + connection windows). 4b can therefore never make a call worse than not
    routing — the worst case is it declines and the direct path runs."""
    if not coalescing_enabled():
        return None
    try:
        vendor = adapters.provider_for(model)
        if not vendor:
            return None
        c = get_coalescer(vendor, model, intent, system=system, reasoning=reasoning, horizon_s=_horizon_s())
        if c is None:
            return None
        fut = c.submit_request(prompt)          # may raise RuntimeError iff a race closed this coalescer → fall back
    except Exception:
        return None                             # setup failed → decline, caller takes the direct path (no double-spend:
    #                                             the request never entered the coalescer)
    return fut.result()                         # resolved exactly once by the coalescer (dict); outside the try so a
    #                                             genuine coalescer error RESULT is returned, never retried as direct


def reset_registry():
    """Drop all shared coalescers (closing each) — for tests and a clean shutdown. Never raises."""
    with _REG_LOCK:
        regs = list(_REG.values())
        _REG.clear()
        _LAST_USED.clear()
    for c in regs:
        try:
            c.close(wait=False)
        except Exception:
            pass
