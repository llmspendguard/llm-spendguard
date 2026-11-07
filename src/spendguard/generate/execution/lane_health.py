"""Manage lane and (lane, model) cooling windows, size-watermark servability and fallback learning, and operator-facing lane down, authentication, and logout surfacing."""

import threading as _threading
import time as _time_surface

from ... import config, resource_state
from spendguard.store.reference_data import reference_data_accessors as _reference_data_accessors


def _pool_cooldown_s():
    import os as _os
    try:
        return float(_os.environ.get("SPENDGUARD_POOL_COOLDOWN_S")
                     or config._cfg_get("advisor", "pool_cooldown_s", 900))
    except Exception:
        return 900.0


def _max_quota_cool_s():
    """Cap on how long a QUOTA/reset-window failure cools a lane. A provider may STATE a huge window ("resets in
    95h"), but a plan whose quota OSCILLATES (agy's Starter quota serves some calls even while reporting a 95h
    reset) must be RE-TESTED periodically, not bypassed for days — the first call after this cap IS the re-test,
    and a success clears the cool. Config advisor.max_quota_cool_s (default 1800s = 30m); env override."""
    import os as _os
    try:
        return float(_os.environ.get("SPENDGUARD_MAX_QUOTA_COOL_S")
                     or config._cfg_get("advisor", "max_quota_cool_s", 1800))
    except Exception:
        return 1800.0


def _lane_cooling(lane):
    return resource_state.cooling(resource_state.lane_key(lane))


def _lane_cool(lane, seconds=None, reason=""):
    """Cool a whole lane for `seconds` (default the pool cooldown), tagged with `reason` (quota / down / failover).
    A quota exhaustion passes its (capped) reset window so the lane stays down until it resets, instead of being
    retried every 900s only to re-fail. Delegates to the unified resource_state store — which persists it so a
    FRESH bulk process honours a still-active cool (a quota exhaustion must outlive the process that hit it)."""
    resource_state.cool(resource_state.lane_key(lane), float(seconds) if seconds else _pool_cooldown_s(), reason)
    # EVENT-DRIVEN health: only a 'down' (the lane AND its metered-API fallback BOTH failed to serve this call) is a
    # candidate for a "this lane needs you" alert — and reliability CONFIRMS it with a bounded probe before alarming,
    # so a TRANSIENT blip under load (that recovers on the next call) never toasts. A 'failover' is deliberately NOT
    # alarmed: a substitute lane CARRIED the work, so the call SUCCEEDED gracefully — the brief backoff cool above is
    # all that's warranted; toasting "lane failing" when the work just succeeded via fallback is a false alarm (the
    # exact flapping the user hit under load). 'quota'/model-miss are transient too (the cool + reset handle them).
    # The reason codes are the ones THIS function was passed (mechanical), never a judgement about an error. Fail-safe.
    if reason == "down":
        try:
            from ... import reliability
            reliability.note_lane_down(lane, reason)
        except Exception:
            pass


class _LaneAnnounceThrottle:
    """Announce a lane's outage to the user ONCE per lane per window (not once per failed task), with STALENESS: a
    later outage after the window re-surfaces. State (per-lane last-announce ts + its lock) is ENCAPSULATED here — a
    method reaches it via `self`, so nothing mutates a hidden module global. Bounded: one entry per lane (the fixed
    registry set). Window matches reliability._EVENT_NOTIFY_THROTTLE_S so the inline line and the macOS notify agree."""

    def __init__(self, window_s=1800.0):
        self._window_s = window_s
        self._at = {}
        self._lock = _threading.Lock()

    def take(self, lane):
        """True (and records `now`) if `lane` may be announced now; False if announced within the window (stale re-fires)."""
        now = _time_surface.monotonic()
        with self._lock:
            last = self._at.get(lane)
            if last is not None and (now - last) < self._window_s:
                return False
            self._at[lane] = now
            return True


_lane_announce = _LaneAnnounceThrottle()


def _surface_lane_down(lane, err):
    """OPERATOR-only, opt-in: a lane could not serve. A lane is spendguard's INTERNAL $0-routing detail — a CONSUMER of
    the library must never see it on the call path (the call fails over to metered and SUCCEEDS; which route served is
    not the consumer's concern). So this is OFF by default and prints nothing; the down lane is still recorded for the
    OPERATOR by _lane_cool → reliability.note_lane_down (surfaced by `spendguard doctor` / `lanes`), and work is never
    lost. An operator watching a live batch can opt in with advisor.lane_call_alerts=on. Best-effort, never raises."""
    try:
        from ... import config as _cfg
        if str(_cfg._cfg_get("advisor", "lane_call_alerts", "off")).lower() not in ("on", "1", "true", "yes", "warn"):
            return                                        # DEFAULT OFF — the consumer never sees a lane on the call path
        if not _lane_announce.take(lane):
            return                                        # already surfaced within the window (a stale entry re-surfaces)
        import sys as _sysd
        from ... import reliability
        hint = reliability.lane_login_hint(lane)
        msg = (f"[spendguard] {lane} lane could not serve (the executor errored): {str(err)[:80]} — cooled and "
               f"rerouting (other $0 lanes first, then metered) so work is not lost.")
        if hint:
            msg += f" If it is a login/token expiry, re-activate: {hint}."
        msg += " Run `spendguard reliability --run --remediate` for the exact fix."
        print(msg, file=_sysd.stderr)
    except Exception:
        pass


def _lane_chatter(msg):
    """OPERATOR-only, opt-in call-path lane commentary — which $0 plan served a call, a fallback to the metered API, a
    lane cooled/rerouted. A lane is spendguard's INTERNAL routing detail: a CONSUMER of the library never needs to see
    it (the call SUCCEEDS regardless of which route served, and cost is reported separately), so this is OFF by default.
    The operator's durable view of lane health is `spendguard doctor` / `lanes` + the recorded lane_health rows
    (reliability.note_lane_*). An operator watching a live batch opts in with advisor.lane_call_alerts=on. Never raises."""
    try:
        from ... import config as _cfg
        if str(_cfg._cfg_get("advisor", "lane_call_alerts", "off")).lower() not in ("on", "1", "true", "yes", "warn"):
            return
        import sys as _sysc
        print(msg, file=_sysc.stderr)
    except Exception:
        pass


def _surface_lane_auth(lane, cmd):
    """OPERATOR-only, opt-in: a $0 lane is CONFIRMED LOGGED OUT. Like _surface_lane_down, a logged-out lane is an
    internal $0-routing detail, not a CONSUMER concern — the call falls back to the metered API and SUCCEEDS. So this is
    OFF by default; the logout is recorded for the OPERATOR by reliability.note_lane_auth_down (a persistent lane_health
    row + a throttled one-time macOS alert that SELF-HEALS on the lane's next success) and shown by `spendguard doctor`.
    An operator can opt into live call-path alerts with advisor.lane_call_alerts=on. Best-effort, never raises."""
    try:
        from ... import config as _cfg
        if str(_cfg._cfg_get("advisor", "lane_call_alerts", "off")).lower() not in ("on", "1", "true", "yes", "warn"):
            return                                        # DEFAULT OFF — the consumer never sees a lane on the call path
        if not _lane_announce.take(lane):
            return
        import sys as _sysd
        msg = (f"[spendguard] ⚠ {lane} lane is LOGGED OUT — its calls now fall back to the METERED API (billed) "
               f"until you re-login.")
        if cmd:
            msg += f" Restore $0: {cmd}"
        else:
            from ... import reliability
            hint = reliability.lane_login_hint(lane)
            if hint:
                msg += f" Re-activate: {hint}"
        print(msg, file=_sysd.stderr)
    except Exception:
        pass


def _authoritative_lane_auth_status(lane):
    """Read current lane auth without the routing cache; logout alerts must never consume stale state."""
    try:
        from spendguard.platform import platform_accessors as _platform_accessors
        return _platform_accessors.resolve_lane_auth_status(lane, ttl=0)
    except Exception:
        return None


def _surface_confirmed_lane_logout(lane, auth_status):
    """Emit and persist logout only for the authoritative checker's exact False verdict."""
    if not isinstance(auth_status, dict) or auth_status.get("authed") is not False:
        return False
    relogin_command = auth_status.get("cmd") or ""
    _surface_lane_auth(lane, relogin_command)
    try:
        from ... import reliability
        reliability.note_lane_auth_down(lane, relogin_command)
    except Exception:
        pass
    return True


def _lane_model_cooling(lane, model):
    return resource_state.cooling(resource_state.lane_model_key(lane, model))


def _lane_model_cool(lane, model):
    """Back off ONE (lane, model) after the lane failed for it WITHIN a proven-good size and the API then answered
    — a MODEL/content mismatch (e.g. codex 400s gpt-5-mini on a ChatGPT plan: "not supported with a ChatGPT
    account"), not the lane being down. Per-model so gpt-5.5 keeps riding codex the whole time; self-healing (it
    expires) so a model the plan later serves is retried. Without this the lane re-intercepts the rejected model on
    EVERY call — the '_learn_from_fallback' size axis alone can't see a model rejection. Its own cooldown axis in
    the resource_state store (a distinct key from the whole-lane cool), tagged 'model-miss'."""
    resource_state.cool(resource_state.lane_model_key(lane, model), _pool_cooldown_s(), "model-miss")


# A lane failure is NOT always the lane being down. A CLI that ran the prompt as an AGENT and hit its turn limit,
# or a prompt that overran the plan model's context, is a PROMPT-vs-lane MISMATCH — the lane is fine for other
# prompts — so cooling the WHOLE lane for 900s (metering even small prompts after it) is the wrong response.
# Measured on a 4-LLM code review: one big file's claude-code result came back error_max_turns and cooled the
# lane. Rather than parse each lane's error TEXT to guess which kind it was (fragile, and every lane words it
# differently), the SYSTEM uses the fact it already has: it falls back to the API anyway, so the API OUTCOME on
# the SAME prompt settles it — the API answered where the lane did not ⇒ the lane was UNSUITABLE for this prompt
# (keep it, and route prompts this size straight to API); the API failed too ⇒ a real problem (cool the lane).
# One signal, every lane, and no interpretation of an error string.
def _lane_note_ok(lane, prompt):
    """Record that the lane ANSWERED a prompt of this size — the proven-good watermark below which an 'unsuitable'
    failure is content-specific, not a size limit, so a small anomaly can never disable a lane that demonstrably
    handles that size. Lives in the resource_state store's size_ceiling axis (persistent, survives processes)."""
    resource_state.note_proven_good(resource_state.lane_key(lane), len(prompt or ""))
    try:                                             # a SERVED call RESOLVES any event-down for this lane — the recovery
        from ... import reliability                     # signal that clears the health row + makes the next down a fresh
        reliability.note_lane_ok(lane)               # blip (not read as a sustained outage). Best-effort; never blocks a call.
    except Exception:
        pass


def _lane_too_big(lane, prompt):
    """True when a prompt this size already provoked a GENUINE size failure on this lane (above its proven-good
    size) — route it straight to the API rather than pay the lane's cold start to fail again. The learned ceiling
    lives in the resource_state store, persisted with a RE-TEST window, so a one-off failure never bypasses the
    lane forever and a real limit is not re-learned from scratch each process (the old in-memory ceiling was)."""
    ceil = resource_state.size_ceiling(resource_state.lane_key(lane))
    return ceil is not None and len(prompt or "") >= ceil


def _learn_from_fallback(lane_name, prompt, api_failed, model=None, transient=False):
    """The auto-route decision from the API-fallback OUTCOME (never the lane's error string). The REASON for the
    miss decides what is learned — throwing the reason away is exactly what let a quota miss disable a lane:
      api_failed=True  → the API ALSO failed ⇒ a real problem: cool the lane. Returns "down".
      transient=True   → the lane failed with a STRUCTURED quota/rate signal (a parsed reset window) ⇒ NOT a size
                         or content limit. The lane was already cooled UNTIL its reset window, so learn NOTHING
                         about suitability and never pin a size ceiling. Returns "transient". (THE FIX: a quota
                         miss used to be indistinguishable from "prompt too big" and permanently bypassed the lane.)
      otherwise the API answered where the lane did not ⇒ unsuitable for THIS prompt. A SIZE ceiling is learned
      ONLY when the failing size is LARGER than a size the lane has PROVEN it can answer (proven_good > 0) — a
      genuine size signal. Without a proven-good baseline the miss is ambiguous (schema/content/a not-yet-parsed
      transient), so back off THIS model on THIS lane (retryable) rather than pin a PERMANENT size ceiling from the
      very first miss — the poison that starved a whole working lane. The signals are FACTS (did the API answer? a
      structured quota token?), never a keyword read of the prose."""
    if api_failed:
        _lane_cool(lane_name, reason="down")
        return "down"
    if transient:
        return "transient"
    n = len(prompt or "")
    _ok = resource_state.proven_good(resource_state.lane_key(lane_name))
    if _ok > 0 and n > _ok:                         # failure ABOVE a proven-good size ⇒ a genuine size limit
        resource_state.set_size_ceiling(resource_state.lane_key(lane_name), n)   # min-ratchet + re-test window
        return "unsuitable"
    if model:                                       # no proven-good baseline (or within it) ⇒ model/content miss,
        # A REPEAT within the cooldown window is a CONFIRMED miss (the first was already backed off and the window has
        # not expired, yet the SAME (lane,model) failed again) — promote THIS specific id into the lane's unserved set
        # so the dispatch PREFLIGHT substitutes it (lane_served_substitute, strong-tier-first — no downgrade) next time
        # instead of re-attempting + bleeding to metered+unpriced on the ~cooldown cycle forever. maybe_refresh_on_failure
        # re-measures the lane's CONFIGURED models, so a caller-supplied id that was never a configured lane model (e.g.
        # glm-5.3-flashx requested on zai-coding, which serves glm-5.3) is otherwise NEVER recorded unserved. First miss
        # stays retryable (no poison from a fluke); the servability RECOVERY probe un-promotes it if the lane serves it
        # again. $0 (a cooling read + a catalog merge); never fails over this bookkeeping.
        _repeat_miss = False
        try:
            _repeat_miss = bool(resource_state.cooling(resource_state.lane_model_key(lane_name, model)))
        except Exception:
            _repeat_miss = False
        _lane_model_cool(lane_name, model)          # retryable — back off THIS model on THIS lane, NOT a permanent
        if _repeat_miss:
            try:
                # UNION with the lane's existing unserved set — merge_lane_observation REPLACES lane_unserved[lane]
                # with what we pass (it is written for a full re-measurement), so passing only [model] would clobber
                # any models already recorded unserved. served=[] keeps the prior served set (minus this model).
                _rej = sorted(set(_reference_data_accessors.resolve_lane_unserved_ids(lane_name) or []) | {model})
                _reference_data_accessors.resolve_merge_lane_observation(lane_name, [], _rej)
            except Exception:
                pass
        try:                                        # a REPEATED model-miss may be a PERMANENT plan drop (codex 0.160.1
            from ... import lane_servability as _ls_fb  # dropped gpt-5.6-sol) — re-measure this lane's servability so the
            _ls_fb.maybe_refresh_on_failure(lane_name)  # drop is recorded + shouted within a throttle window, not all day
        except Exception:
            pass                                    # the trigger is a bonus; a miss must never fail over it
    return "model-cooled"                           # size ceiling from an ambiguous first miss
