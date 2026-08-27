"""Per-resource STATE — the single, persisted, multi-axis store for what is currently true about each lane and
each metered (provider, model). It replaces the scattered in-memory flags (_lane_cooldown, _lane_model_cooldown,
later _lane_big_prompt_ceiling / _lane_ok_max / the output-ceiling resolution) whose reason-BLINDNESS kept letting
a transient failure masquerade as a permanent limit and bypass a working lane.

A resource is a namespaced KEY, so the axes' different granularities coexist in one store:
  lane:<lane>                  — whole-lane axes (cooldown; later size_ceiling)
  lane:<lane>/model:<model>    — per-(lane,model) axes (a model this lane rejected)
  model:<provider>:<model>     — per-model axes (later output_ceiling)

MULTI-STATE-ABLE by design: a resource can be several things at once (cooling AND size-limited), so each axis is
INDEPENDENT, carries its own REASON, and has its own next-retest time — the router reads the axes it cares about.

AXIS 1 — COOLDOWN {until, reason}: the resource is unavailable until `until`; `reason` (quota/rate/down/model-miss)
is what the old code threw away. Persisted to SPENDGUARD_HOME/resource_state.json (atomic, +backup) so a fresh
process honours a still-active cooldown; expired cooldowns are dropped on load. This is a STRANGLER migration: the
adapters helpers (_lane_cool/_lane_cooling/…) keep their exact behaviour and now delegate here.
"""
import time
import threading

from . import config

_STATE_NAME = "resource_state"   # persisted under SPENDGUARD_HOME via the shared config.load_state/save_state
_lock = threading.Lock()
_state = {}                      # key -> {"cooldown": {"until": ts, "reason": str}, ...more axes later...}


def _prune(d):
    """Drop dead cooldowns (and any resource left with no live axis) — a stale 'until' must never read as cooling."""
    now = time.time()
    out = {}
    for key, rec in (d or {}).items():
        rec = dict(rec) if isinstance(rec, dict) else {}
        cd = rec.get("cooldown")
        if isinstance(cd, dict) and float(cd.get("until") or 0) <= now:
            rec.pop("cooldown", None)
        if rec:
            out[key] = rec
    return out


def _load_state():
    """Reload the persisted store; drop expired cooldowns. Uses the SHARED config.load_state — the same per-module
    persistence the other _load_state adapters use, and whose save_state refuses to overwrite a file it could not
    read (so a corrupt file is preserved, never clobbered into 'no cooldowns')."""
    global _state
    _state = _prune(config.load_state(_STATE_NAME, {}))


def _save():
    config.save_state(_STATE_NAME, _prune(_state), loud=False)     # per-module persisted state; quiet (frequent writes)


def _rec(key):
    return _state.setdefault(key, {})


# ── AXIS 1: COOLDOWN {until, reason} ─────────────────────────────────────────────────────────────────────────
def set_cooldown(key, until_ts, reason=""):
    """Mark a resource cooling until `until_ts` (unix seconds) for `reason`. A LATER 'until' extends; an earlier
    one never shortens an active cool (a transient rate-limit can't undercut a quota window)."""
    with _lock:
        cd = _rec(key).get("cooldown") or {}
        if float(until_ts) > float(cd.get("until") or 0):
            _rec(key)["cooldown"] = {"until": float(until_ts), "reason": reason or cd.get("reason") or ""}
            _save()


def cool(key, seconds, reason=""):
    """Convenience: cool for `seconds` from now."""
    set_cooldown(key, time.time() + float(seconds), reason)


def cool_until(key):
    """The unix ts this resource is cooling until, or 0.0 if not cooling."""
    cd = (_state.get(key) or {}).get("cooldown") or {}
    u = float(cd.get("until") or 0)
    return u if u > time.time() else 0.0


def cool_reason(key):
    """Why it is cooling ('quota'/'rate'/'down'/…), or '' if not cooling."""
    if not cool_until(key):
        return ""
    return ((_state.get(key) or {}).get("cooldown") or {}).get("reason") or ""


def cooling(key):
    return cool_until(key) > 0.0


def clear_cooldown(key):
    with _lock:
        rec = _state.get(key)
        if rec and rec.pop("cooldown", None) is not None:
            if not rec:
                _state.pop(key, None)
            _save()


# ── keys + test/reset ────────────────────────────────────────────────────────────────────────────────────────
def lane_key(lane):
    return f"lane:{lane}"


def lane_model_key(lane, model):
    return f"lane:{lane}/model:{model}"


def model_key(provider, model):
    return f"model:{provider}:{model}"


def _reset():
    """Drop all in-memory state — for isolated tests only (does not touch disk)."""
    with _lock:
        _state.clear()


_load_state()   # honour a still-active cooldown from a previous process (e.g. a quota reset window)
