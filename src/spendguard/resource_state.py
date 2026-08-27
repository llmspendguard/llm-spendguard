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
    """Drop dead cooldowns AND stale size-ceilings (past their re-test window), plus any resource left with no live
    axis. A stale 'until' must never read as cooling, and a size ceiling past its re-test window must not bypass a
    lane forever — but the proven-good watermark it carries is a positive fact and is KEPT."""
    now = time.time()
    out = {}
    for key, rec in (d or {}).items():
        rec = dict(rec) if isinstance(rec, dict) else {}
        cd = rec.get("cooldown")
        if isinstance(cd, dict) and _num(cd.get("until")) <= now:      # malformed 'until' → treat as expired (safe)
            rec.pop("cooldown", None)
        sc = rec.get("size_ceiling")
        if isinstance(sc, dict) and sc.get("value") is not None and _num(sc.get("until")) <= now:
            kept = {k: v for k, v in sc.items() if k == "proven_good"}   # re-test window passed → drop the ceiling,
            rec["size_ceiling"] = kept if kept else None                 # KEEP the proven-good watermark
            if not rec["size_ceiling"]:
                rec.pop("size_ceiling", None)
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


def _num(x, default=0.0):
    """A persisted numeric field coerced to float, defaulting on ANY malformed value. The store is consulted on the
    hot dispatch path from a file that could be truncated or hand-edited; a stored 'until' of 'not-a-number' must
    read as an expired/absent axis, never raise ValueError and crash every call that consults it."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def _int_or_none(x):
    """A persisted COUNT coerced to int, or None when absent/malformed. Deliberately None (not 0) for a bad value:
    a size ceiling read as 0 would route EVERY prompt to the API (the poison direction that starves a lane), so a
    corrupt ceiling must read as ABSENT — the lane is tried — not as a 0-char limit that disables it."""
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ── AXIS 1: COOLDOWN {until, reason} ─────────────────────────────────────────────────────────────────────────
def set_cooldown(key, until_ts, reason=""):
    """Mark a resource cooling until `until_ts` (unix seconds) for `reason`. A LATER 'until' extends; an earlier
    one never shortens an active cool (a transient rate-limit can't undercut a quota window)."""
    with _lock:
        cd = _rec(key).get("cooldown") or {}
        if float(until_ts) > _num(cd.get("until")):        # stored 'until' coerced safely (corrupt → treated as 0)
            _rec(key)["cooldown"] = {"until": float(until_ts), "reason": reason or cd.get("reason") or ""}
            _save()


def cool(key, seconds, reason=""):
    """Convenience: cool for `seconds` from now."""
    set_cooldown(key, time.time() + float(seconds), reason)


def cool_until(key):
    """The unix ts this resource is cooling until, or 0.0 if not cooling."""
    cd = (_state.get(key) or {}).get("cooldown") or {}
    u = _num(cd.get("until"))
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


# ── AXIS 2: SIZE_CEILING {value, until, proven_good} — a lane's prompt-CHARS suitability ──────────────────────
def _size_retest_s():
    """How long a learned size-ceiling stays authoritative before it is RE-TESTED — the 'when to re-test' the
    resource model needs so a one-off failure never bypasses a lane forever. Config advisor.size_ceiling_retest_s
    (default 6h); env override. After it, size_ceiling() reports None so the next big prompt tries the lane again."""
    import os
    try:
        return float(os.environ.get("SPENDGUARD_SIZE_CEILING_RETEST_S")
                     or config._cfg_get("advisor", "size_ceiling_retest_s", 21600))
    except Exception:
        return 21600.0


def set_size_ceiling(key, chars):
    """Learn this lane is unsuitable at/above `chars` prompt-characters — a GENUINE size limit (the caller only
    sets it ABOVE a proven-good size). Min-ratchet (a smaller failing size lowers it), stamped with a re-test
    window. Persisted so a real limit is not re-learned from scratch each process (the old in-memory ceiling was)."""
    with _lock:
        sc = _rec(key).get("size_ceiling") or {}
        cur = _int_or_none(sc.get("value"))               # corrupt stored ceiling → treated as unset, then re-set
        sc["value"] = int(chars) if cur is None else min(cur, int(chars))
        sc["until"] = time.time() + _size_retest_s()
        _rec(key)["size_ceiling"] = sc
        _save()


def size_ceiling(key):
    """The ACTIVE size ceiling (chars), or None when unset OR past its re-test window (stale → try the lane again).
    The proven-good watermark is separate and never expires."""
    sc = (_state.get(key) or {}).get("size_ceiling") or {}
    v = _int_or_none(sc.get("value"))                     # corrupt value → None (ABSENT: try the lane), never 0
    if v is None or time.time() >= _num(sc.get("until")):
        return None
    return v


def note_proven_good(key, chars):
    """Raise the proven-good watermark — the largest prompt this lane has SUCCESSFULLY answered. Persistent (a
    positive fact), and the baseline below which a failure is content-specific, not a size limit."""
    with _lock:
        sc = _rec(key).get("size_ceiling") or {}
        if int(chars) > (_int_or_none(sc.get("proven_good")) or 0):    # corrupt watermark → treated as 0 (safe)
            sc["proven_good"] = int(chars)
            _rec(key)["size_ceiling"] = sc
            _save()


def proven_good(key):
    return _int_or_none(((_state.get(key) or {}).get("size_ceiling") or {}).get("proven_good")) or 0


def clear_size_ceiling(key):
    """Drop the ACTIVE size ceiling (value + re-test window) — the twin of clear_cooldown — while KEEPING the
    proven-good watermark (a positive fact). Used when a lane should be re-tested at a size it was ceilinged at
    (e.g. the limit was transient after all), without forgetting the largest size it has demonstrably handled."""
    with _lock:
        rec = _state.get(key)
        sc = (rec or {}).get("size_ceiling")
        if not isinstance(sc, dict):
            return
        changed = sc.pop("value", None) is not None
        sc.pop("until", None)
        if not sc:                                  # nothing left (no proven_good) → drop the axis, maybe the record
            rec.pop("size_ceiling", None)
            if not rec:
                _state.pop(key, None)
        if changed:
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
