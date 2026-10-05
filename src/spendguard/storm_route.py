"""4b — the OTHER admission door. bulk_delegate/submit_storm are the EXPLICIT fan entries; the actual 429-storm caller
was the IMPLICIT one: honestreview's own ThreadPoolExecutor firing many independent adapters.call(metered_only) at one
vendor. 4b routes that raw concurrent traffic through ONE shared StormCoalescer per identical call-shape, so the
implicit fan gets the same pace+batch COMBO as the explicit entries — pacing the sustainable realtime share under the
(xp_rate-governed) wall and diverting the overflow to the Batch API.

FEATURE-FLAGGED (SPENDGUARD_STORM_COALESCE), default OFF: it changes the hot adapters.call path, so it lands DARK,
is proven on a both-doors replay, then enabled.

Keyed by the FULL call shape (vendor, model, intent, reasoning, system): a shared coalescer bakes system+reasoning
into its realtime + batch executors, so only calls that are identical on those may coalesce (the incident fan is —
same intent/model/reasoning/system); any difference gets its own coalescer (correctness over maximal coalescing). The
realtime executor re-enters adapters.call with _route=False — the recursion guard bulk_delegate already uses — so a
routed call still rides the governed metered path (dispatch.admit → the cross-process rate window → _call_guarded). A
lone call hits quiescence and resolves fast, so routing is safe for non-storm traffic too.
"""
import hashlib
import os
import threading

from . import adapters, dispatch
from .storm_coalescer import StormCoalescer
from .storm_submit import default_batch_executor

_REG = {}
_REG_LOCK = threading.Lock()
_DEFAULT_HORIZON_S = 30.0


def coalescing_enabled():
    """4b routing is opt-in (default OFF) — it changes the hot adapters.call path, so it is proven dark first."""
    return os.environ.get("SPENDGUARD_STORM_COALESCE") == "1"


def _horizon_s():
    """The urgency horizon that sizes each coalescer's realtime budget (rate × horizon). Overridable via
    SPENDGUARD_STORM_HORIZON_S; defaults to _DEFAULT_HORIZON_S."""
    try:
        v = float(os.environ.get("SPENDGUARD_STORM_HORIZON_S") or _DEFAULT_HORIZON_S)
        return v if v > 0 else _DEFAULT_HORIZON_S
    except (TypeError, ValueError):
        return _DEFAULT_HORIZON_S


def _rate_for(vendor, model):
    el = dispatch.effective_limits(vendor, model) or {}
    rpm = int(el.get("rpm") or 0)
    return (rpm / 60.0) if rpm > 0 else None


def _coalescer_key(vendor, model, intent, reasoning, system):
    sysh = hashlib.sha1((system or "").encode("utf-8")).hexdigest()[:12]   # a stable, compact system fingerprint
    return (vendor, model, intent or "", reasoning or "", sysh)


def get_coalescer(vendor, model, intent, system=None, reasoning="minimal", horizon_s=_DEFAULT_HORIZON_S):
    """The shared StormCoalescer for one exact call shape (created lazily). Returns None when no rpm is known for the
    vendor (nothing to pace against → the caller runs its normal direct path)."""
    rate = _rate_for(vendor, model)
    if rate is None:
        return None
    key = _coalescer_key(vendor, model, intent, reasoning, system)
    with _REG_LOCK:
        c = _REG.get(key)
        if c is None:
            def _run_routed_realtime(prompt, _m=model, _s=system, _r=reasoning, _i=intent, _h=horizon_s):
                return adapters.call(_m, prompt, system=_s, reasoning=_r, sig=_i, metered_only=True,
                                     governed=True, no_substitution=True, _route=False, timeout_s=_h)
            c = StormCoalescer(
                execute_realtime=_run_routed_realtime,
                execute_batch=default_batch_executor(model, intent, system=system, reasoning=reasoning),
                sustainable_rate_per_s=rate, horizon_s=horizon_s, provider=vendor)
            _REG[key] = c
        return c


def route(model, prompt, intent, system=None, reasoning="minimal"):
    """Submit one labelled call into the shared coalescer for its exact shape and BLOCK on the result. Returns the
    result dict — or None when routing is not applicable (routing disabled, or no known rate), so the caller falls
    back to its normal direct path."""
    if not coalescing_enabled():
        return None
    vendor = adapters.provider_for(model)
    if not vendor:
        return None
    c = get_coalescer(vendor, model, intent, system=system, reasoning=reasoning, horizon_s=_horizon_s())
    if c is None:
        return None
    return c.submit_request(prompt).result()


def reset_registry():
    """Drop all shared coalescers (closing each) — for tests and a clean shutdown. Never raises."""
    with _REG_LOCK:
        regs = list(_REG.values())
        _REG.clear()
    for c in regs:
        try:
            c.close(wait=False)
        except Exception:
            pass
