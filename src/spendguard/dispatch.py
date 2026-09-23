"""The DISPATCH GOVERNOR — bounded concurrency + optional rate pacing per vendor/lane, so cross-LLM work at
scale QUEUES instead of thrashing or 429-storming.

Why this exists (the gap `fan_out` alone leaves). `fan_out` spawns one ThreadPoolExecutor per call sized to the
vendor count. That is correct for ONE panel of four. It is wrong the moment a caller runs many panels at once —
honestreview reviews 50 files x 4 vendors, and naive fan-out puts ~200 calls in flight simultaneously. Two
distinct things then break, and neither is a bug in fan_out:
  * the SUBSCRIPTION LANES are subprocess-based (claude-code spawns `claude`, codex spawns `codex`, each paying a
    cold start + a ~14K-token context injection). Fifty concurrent CLI processes on one plan is slower than a
    small pool and trips the plan's own concurrency throttle — measured as lane cooldowns cascading.
  * the METERED vendors hit provider RPM/TPM limits and return 429s, which the transport path then RETRIES,
    turning a rate limit into a latency multiplier.

The fix is an ADMISSION layer that is separate from accounting (the ledger, done) and from the hard-cap refusal
(the gate, done). This is LAYER 3 — SCHEDULING: per (vendor | lane) it bounds how many calls are in flight, and
optionally how many start per minute, and makes the overflow WAIT rather than fire or fail. It is the LiteLLM
Router idea (rpm/tpm/max-parallel per deployment) sized to this codebase — in-process, because the work is one
job's I/O fan-out, not a distributed queue; a broker would be a different product and rebuild what a bounded
semaphore already does.

KEYING. Vendors that ride the SAME subscription lane share ONE budget — one plan, one CLI, one throttle — so the
key is the lane name when `adapters._lane_for(vendor)` is active, else the vendor. That is the whole reason a
lane and a vendor cannot share a concurrency counter.

HONESTY UNDER LOAD. Waiting for a slot counts against the caller's deadline (a caller who asks for an answer in
60s means end-to-end, queue time included). If no slot frees within the deadline, acquire() raises
DispatchTimeout, which vendor_call maps to DEADLINE_EXCEEDED — a queued call that never ran is a failure with a
reason, never a silent success. Same invariant as everywhere else here.

Limits are NAMED DEFAULTS + config/env overrides — never a literal at a call site. Conservative by default: the
concurrency bound protects the lanes, RPM pacing is off (0) until a deployment sets a real number, and a
four-vendor panel is unconstrained by either.
"""
import os
import threading
import time

# ── Default limits (named, overridable via config `dispatch.*` or env `SPENDGUARD_DISPATCH_*`) ───────────────
# A subscription lane is a heavy subprocess (CLI cold-start + context injection); a handful in flight beats a
# swarm. A metered vendor is a plain HTTPS call and tolerates more. Neither number is keyed to a model — they
# are the admission budget for a whole lane / a whole vendor, and a 4-vendor panel touches neither.
DEFAULT_LANE_CONCURRENCY = 8       # max concurrent calls sharing one subscription lane (claude-code/codex/zai).
# MEASURED plan ceilings are ~12 concurrent (codex ≥12 with 0 errors; zai Max ~12, hard 429 at 16), so 8 exploits
# the plan with headroom; a 429/quota still trips the per-lane cooldown (and zai retries 429 with backoff), so
# overshoot self-corrects. Per-lane override: dispatch.lane_concurrency_<lane> (below). Was 3 — a pre-measurement
# floor that throttled the whole cross-lane fan below every plan's real concurrency (the "governor throttle").
DEFAULT_VENDOR_CONCURRENCY = 8     # max concurrent metered calls to one vendor
DEFAULT_RPM = 0                    # requests/minute per key; 0 = pacing OFF (only concurrency governs). Opt-in.
DEFAULT_TPM = 0                    # tokens/minute per key (input+output); 0 = pacing OFF. The quantity that ACTUALLY
# 429s on OpenAI/Anthropic (an enqueued-/tokens-per-minute ceiling), which a concurrency or RPM cap cannot bound — a
# handful of big-context calls blow TPM while well under both. Seeded from each provider's real limit via config
# `dispatch.tpm_<vendor>` (never a literal here); 0 until set, then Step-3 self-calibration learns it from 429 headers.
DEFAULT_GLOBAL_CONCURRENCY = 24    # a machine-wide ceiling across ALL keys — the last backstop against a swarm

_ENV_PREFIX = "SPENDGUARD_DISPATCH_"


def _off():
    """Kill switch: SPENDGUARD_DISPATCH_OFF=1 makes every acquire a no-op. The safety valve if the governor is
    ever suspected of holding a call — spend discipline must never depend on a scheduler being correct."""
    return os.environ.get(_ENV_PREFIX + "OFF") == "1"


def _limit(key, default):
    """A dispatch limit: env `SPENDGUARD_DISPATCH_<KEY>` wins, then config `dispatch.<key>`, then the default.
    Parsed as int; a malformed value falls back to the default rather than raising into a hot path."""
    env = os.environ.get(_ENV_PREFIX + key.upper())
    if env is not None:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    try:
        from . import config
        v = config._cfg_get("dispatch", key, default)
        return int(v)
    except Exception:
        return int(default)


# ── SELF-CALIBRATION — learn a vendor's REAL rate limit from provider 429 headers ─────────────────────────────────
# Admission's whole point is 'no user ever sees a 429', but TPM/RPM pacing only bites once a per-vendor limit EXISTS.
# This closes that gap with zero manual config: when a metered call returns 429, adapters reads the provider's own
# rate-limit headers (x-ratelimit-limit-tokens / anthropic-ratelimit-tokens-limit, and Retry-After) and teaches them
# here — dispatch then PACES to the learned limit, so the first limit-unknown 429 is the LAST. The store is in-memory
# for the per-call hot path AND a JSON file, so it survives restarts and one process's lesson reaches the others.
# Learning a LIMIT is parsing a fixed-format header into an int — never a meaning judgement.
_LEARN_RELOAD_S = 30.0
# ANTI-TRAP (a learned rate must NEVER trap us on a wrong/intermittent reading — two defenses):
#   A. LATEST-WINS: every observation OVERWRITES the prior (learn() SETS, never min-latches), and success headers
#      re-observe the real limit on EVERY call — so a transient/one-off low reading is corrected by the very next
#      normal call. This is what makes an INTERMITTENT bad limit self-heal instead of latching.
#   C. COOLDOWN CAP: a provider's Retry-After is honored but CAPPED (_cool_cap_s below), so a bad/huge value (a
#      misbehaving provider sending Retry-After: 86400) can never wedge a vendor in an endless cooldown.
_COOL_CAP_S_DEFAULT = 300          # 5m ceiling on any single Retry-After cooldown; override dispatch.cooldown_cap_s.


def _warn_learned_io(what, e):
    """Say ONCE (per process, per direction) that learned-rate-limit persistence hit an error. The store is fail-open
    by design — a corrupt/racing file must NEVER break admission (spend discipline cannot depend on the scheduler) —
    but the doctrine here is degrade LOUDLY, not silently: a persistently bad file has to be visible, not a stale limit
    quietly in force. Once, not per-call, so a broken disk never spams the log."""
    flag = f"_warned_{what}"
    if getattr(_LearnedLimits, flag, False):
        return
    setattr(_LearnedLimits, flag, True)
    import sys as _sys
    print(f"[spendguard] dispatch: learned rate-limit {what} failed ({type(e).__name__}: {str(e)[:80]}) — keeping "
          f"the in-memory limits and continuing; fix the file under $SPENDGUARD_HOME/dispatch/", file=_sys.stderr)


class _LearnedLimits:
    """Durable per-vendor {tpm, rpm} learned from provider rate-limit headers. get() serves the hot path from memory
    (reloading the JSON only when its mtime changed, and at most every _LEARN_RELOAD_S); learn() merges + ATOMICALLY
    rewrites the file (temp + os.replace) so a concurrent reader never sees a torn write. Fail-OPEN but LOUD-ONCE: an
    I/O or parse error keeps the in-memory limits and warns once (_warn_learned_io), never breaks admission."""
    __slots__ = ("_d", "_checked", "_lock")
    _warned_read = False        # class flags for the loud-once warnings (not instance slots)
    _warned_write = False

    def __init__(self):
        self._d = {}
        self._checked = 0.0     # monotonic time of the last file read; 0.0 = never loaded (see _maybe_reload)
        self._lock = threading.Lock()

    def _path(self):
        from . import config
        d = config.HOME / "dispatch"
        d.mkdir(parents=True, exist_ok=True)
        return d / "learned_limits.json"

    def _maybe_reload(self):
        # Re-read the (small) file at most every _LEARN_RELOAD_S — NOT per call, and NOT gated on mtime: a restore/sync
        # can reset an mtime BACKWARD to a value already seen, so 'mtime unchanged' is not 'content unchanged' (the
        # classic staleness trap). Reading the whole small map on the TTL tick is cheap and cannot be fooled that way.
        # `_checked` doubles as the 'loaded at least once' flag (0.0 = never), so no separate state is needed.
        now = time.monotonic()
        if self._checked and (now - self._checked) < _LEARN_RELOAD_S:
            return
        self._checked = now
        try:
            import json as _json
            self._d = _json.loads(self._path().read_text() or "{}") or {}
        except FileNotFoundError:
            pass                                         # absent == empty; _checked is set so we won't re-read until TTL
        except Exception as e:
            _warn_learned_io("read", e)                  # corrupt/racing file → keep memory, say so ONCE (fail-open+loud)

    def for_vendor(self, vendor):
        self._maybe_reload()
        return self._d.get((vendor or "").strip().lower()) or {}

    def learn(self, vendor, tpm=None, rpm=None, source=""):
        v = (vendor or "").strip().lower()
        if not v or (not tpm and not rpm):
            return

        def _stamp_vendor_limits(data):                  # edits the on-disk map in place — preserves OTHER vendors'
            cur = dict((data.get(v) if isinstance(data, dict) else None) or {})   # entries a concurrent process wrote
            if tpm and int(tpm) > 0:
                cur["tpm"] = int(tpm)
            if rpm and int(rpm) > 0:
                cur["rpm"] = int(rpm)
            cur["source"], cur["ts"] = source, time.time()
            data[v] = cur
            return data
        with self._lock:
            self._maybe_reload()
            _prev = self._d.get(v) or {}
            _prev_tpm, _prev_rpm = _prev.get("tpm"), _prev.get("rpm")
            _stamp_vendor_limits(self._d)                # MEMORY holds the freshest lesson (always)
            _now = self._d.get(v) or {}
            if _now.get("tpm") == _prev_tpm and _now.get("rpm") == _prev_rpm:
                return                                   # the LIMIT is unchanged → memory is enough; skip the durable
                #                                          write. success headers repeat the same limit on EVERY call, so
                #                                          this persists ONCE per (vendor, limit), never a per-call backup.
            try:                                         # persist through the ONE backed-up, atomic JSON writer
                from . import config
                out = config.update_json(self._path(), _stamp_vendor_limits, reason="learn-rate-limit")
                if isinstance(out, dict):
                    self._d = out                        # adopt the merged on-disk state (incl. other processes' vendors)
            except Exception as e:
                from . import gate as _gate
                if _gate.is_deliberate_stop(e):
                    raise                                # a spend refusal / ledger LOCK is a DELIBERATE stop — PROPAGATE,
                _warn_learned_io("write", e)             # never downgrade. A transient IO/lock: memory holds the lesson.


_LEARNED = _LearnedLimits()

# Reactive back-off: a 429's Retry-After cools THAT vendor's new admissions for that long. Process-local (in-memory) —
# the durable, cross-process half is the learned LIMIT above; this is the immediate 'we are over RIGHT NOW' window.
_COOL_UNTIL = {}
_COOL_LOCK = threading.Lock()


def _cool_vendor(vendor, seconds):
    if not seconds or float(seconds) <= 0:
        return
    v = (vendor or "").strip().lower()
    with _COOL_LOCK:
        _COOL_UNTIL[v] = max(_COOL_UNTIL.get(v, 0.0), time.monotonic() + float(seconds))


def _cooldown_left(vendor):
    v = (vendor or "").strip().lower()
    with _COOL_LOCK:
        return max(0.0, _COOL_UNTIL.get(v, 0.0) - time.monotonic())


def learn_rate_limit(vendor, tpm=None, rpm=None, retry_after_s=None, source="429-header"):
    """Teach the governor a vendor's REAL rate limit, learned from a provider 429. `tpm`/`rpm` (from the provider's
    x-ratelimit-limit-* headers) become that vendor's paced ceiling whenever no explicit dispatch.tpm_/rpm_ is set —
    so the first limit-unknown 429 is the LAST. `retry_after_s` (the 429's Retry-After) cools this vendor's new
    admissions for that long — the immediate back-off while the learned limit takes over. Called by adapters on every
    metered 429; safe with partial data (missing fields ignored), and never raises into the caller."""
    _LEARNED.learn(vendor, tpm=tpm, rpm=rpm, source=source)
    if retry_after_s:
        # defense C: honor the provider's Retry-After but CAP it (dispatch.cooldown_cap_s) so a bad/huge value can't
        # wedge this vendor in an endless cooldown. NOT silent: when it caps, say so — a GENUINELY long cool is not
        # lost, it simply re-cools on the NEXT 429 (each cycle capped), and an operator who sees this can raise
        # dispatch.cooldown_cap_s to honor a real long window fully. So a bad value can't trap us AND a real one isn't
        # silently discarded — it degrades to 'poll every cap seconds' with a visible reason.
        _cap = float(_limit("cooldown_cap_s", _COOL_CAP_S_DEFAULT))
        _ra = float(retry_after_s)
        if _ra > _cap:
            import sys as _sys
            print(f"[spendguard] dispatch: {vendor} asked for a {_ra:.0f}s cooldown (Retry-After) — CAPPING at "
                  f"{_cap:.0f}s (dispatch.cooldown_cap_s); a real long window then re-cools on the next 429, raise "
                  f"the cap to honor it in one shot", file=_sys.stderr)
        _cool_vendor(vendor, min(_ra, _cap))


def learned_limits(vendor=None):
    """The learned per-vendor limits, for observability / a receipt / a test: one vendor's {tpm,rpm,source,ts}, or the
    whole map when vendor is None."""
    if vendor is not None:
        return dict(_LEARNED.for_vendor(vendor))
    _LEARNED._maybe_reload()
    return {k: dict(v) for k, v in _LEARNED._d.items()}


class DispatchTimeout(RuntimeError):
    """No dispatch slot for this key became free within the caller's deadline. Raised, never swallowed — the
    caller (vendor_call.call) turns it into a DEADLINE_EXCEEDED Result so a queued-out call is an honest
    failure, not an empty success."""


# ── Cross-process admission (flock slot-files) ──────────────────────────────────────────────────────────────
# The in-process semaphores bound ONE interpreter. But two separate runs on the same machine — a `spendguard ask`
# and a honestreview panel, or two panels — still share ONE subscription plan per lane (one Max, one Codex, one
# GLM) AND one rate-limited metered account per vendor (one OpenAI org, one Anthropic key), and nothing above
# co-governs them, so both could blast the shared resource at once. flock slot-files close that: `limit` lock files
# per key, one flock held for each call's duration. flock is advisory, tied to the fd, and the OS drops it when the
# process dies — so a crash never leaves a stale slot to reap. BOTH lane keys (the shared subprocess plans) AND
# metered vendor keys (per-provider, so N runs share one cap and don't 429-storm the account) are cross-gated. The
# slot count is that key's `limit` (per-lane / per-vendor tunable). Fail-open where fcntl is absent (non-POSIX) —
# the in-process bound still holds. SPENDGUARD_DISPATCH_XP_OFF=1 disables this whole cross-process layer.
try:
    import fcntl as _fcntl
except ImportError:                              # pragma: no cover - non-POSIX (Windows): in-process bound only
    _fcntl = None

_HELD = threading.local()        # per-thread stack of held cross-process slots (or None): acquire pushes, release pops


def _held():
    s = getattr(_HELD, "stack", None)
    if s is None:
        s = _HELD.stack = []
    return s


def _pop_held():
    s = _held()
    return s.pop() if s else None


def _xp_off():
    """Cross-process gating disabled — no fcntl (non-POSIX), or SPENDGUARD_DISPATCH_XP_OFF=1."""
    return _fcntl is None or os.environ.get(_ENV_PREFIX + "XP_OFF") == "1"


class _XPSlot:
    """One held cross-process lane slot — an flock on one of the key's slot files. Released on the call's exit,
    or by the OS if this process dies (flock is bound to the fd, so there are no stale slots to reap)."""
    __slots__ = ("fd",)

    def __init__(self, fd):
        self.fd = fd

    def release(self):
        try:
            _fcntl.flock(self.fd, _fcntl.LOCK_UN)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


def _xp_dir(key):
    import hashlib
    from . import config
    d = config.HOME / "dispatch" / hashlib.sha1(key.encode()).hexdigest()[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _acquire_xp(key, limit, deadline_s):
    """Hold one of `limit` cross-process flock slots for `key`, within deadline_s. Returns an _XPSlot, or raises
    DispatchTimeout if every slot is held by OTHER processes for the whole deadline."""
    d = _xp_dir(key)
    t0 = time.monotonic()
    while True:
        for i in range(max(1, int(limit))):
            fd = os.open(str(d / f"slot-{i}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return _XPSlot(fd)
            except OSError:
                os.close(fd)                     # slot held by another process — try the next
        left = float(deadline_s) - (time.monotonic() - t0)
        if left <= 0:
            raise DispatchTimeout(f"'{key}' cross-process lane full ({int(limit)} slots held by other "
                                  f"processes) — deadline {float(deadline_s):.0f}s exhausted")
        time.sleep(min(0.1, left))


class _Bucket:
    """One key's admission state: a bounded-concurrency semaphore + optional requests/minute AND tokens/minute token
    buckets. Thread-safe. Both buckets refill continuously from monotonic time — no background thread, no cron."""

    __slots__ = ("key", "limit", "rpm", "tpm", "reserve", "_sem", "_sem_batch", "_lock", "_tokens", "_last",
                 "_tpm_tokens", "_tpm_last", "in_flight", "waiting")

    def __init__(self, key, limit, rpm, reserve=0, tpm=0):
        self.key = key
        self.limit = max(1, int(limit))
        # REALTIME RESERVE: hold back `reserve` of this key's slots for interactive/realtime work so a batch fan can
        # never starve a realtime call of a governor slot (the admission twin of the interactive lane reserve). Capped
        # at limit-1 so batch always keeps ≥1 slot (a reserve == limit would deadlock batch forever). 0 (the default)
        # means NO reservation and the batch path is byte-identical to the realtime path — zero behaviour change.
        self.reserve = max(0, min(int(reserve or 0), self.limit - 1))
        self.rpm = max(0, int(rpm))
        self.tpm = max(0, int(tpm))
        self._sem = threading.BoundedSemaphore(self.limit)                # total concurrency — EVERY caller takes this
        # the batch SUB-limit: a batch caller must take this BEFORE _sem, so at most (limit-reserve) batch calls ever
        # hold _sem and ≥ reserve of _sem's permits stay reachable by realtime. None when reserve==0 (no gate at all).
        self._sem_batch = threading.BoundedSemaphore(self.limit - self.reserve) if self.reserve > 0 else None
        self._lock = threading.Lock()
        self._tokens = float(self.rpm)     # start full so the first burst up to rpm is not paced
        self._last = time.monotonic()
        self._tpm_tokens = float(self.tpm)  # token/minute bucket, same continuous refill; starts full
        self._tpm_last = time.monotonic()
        self.in_flight = 0                 # under _lock, for stats/observability
        self.waiting = 0

    def _rpm_wait_s(self):
        """Seconds to wait for one RPM token (0 if a token is available now or pacing is off). Consumes the
        token when it grants immediately; when it must wait, the caller sleeps and re-checks."""
        if self.rpm <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            self._tokens = min(float(self.rpm), self._tokens + (now - self._last) * (self.rpm / 60.0))
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            deficit = 1.0 - self._tokens
            return deficit / (self.rpm / 60.0)

    def _tpm_wait_s(self, tokens):
        """Seconds to wait until `tokens` (this call's estimated input+output) fit under the TPM bucket (0 if capacity
        now or pacing off). RESERVES the tokens when it grants immediately; when it must wait, it reserves nothing —
        the caller sleeps and re-checks (same soft-pacer contract as _rpm_wait_s). A single call LARGER than the whole
        per-minute budget can never fit, so it is admitted immediately (driving the bucket negative, which the next
        calls then wait off) rather than deadlocking forever on a token count it can never reach."""
        t = max(0, int(tokens or 0))
        if self.tpm <= 0 or t <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            self._tpm_tokens = min(float(self.tpm), self._tpm_tokens + (now - self._tpm_last) * (self.tpm / 60.0))
            self._tpm_last = now
            if self._tpm_tokens >= t or t > self.tpm:    # capacity now, OR STRICTLY bigger than the whole budget (can
                self._tpm_tokens -= t                    # never fit even a full bucket) → admit now, never deadlock. A
                return 0.0                               # call EQUAL to the budget still paces (waits a full refill).
            deficit = t - self._tpm_tokens
            return deficit / (self.tpm / 60.0)

    def acquire(self, deadline_s, sla_class=None, est_tokens=0):
        """Block until a concurrency slot is free AND (if paced) an RPM token is available, within deadline_s.
        Returns seconds waited. Raises DispatchTimeout if the deadline passes first. sla_class="batch" (with a reserve
        configured) first takes the batch SUB-limit, so it can never consume a realtime-reserved slot; any other value
        (the default) is realtime and uses the full limit. Lock order is fixed (batch: _sem_batch → _sem; realtime:
        _sem only), so the two paths cannot deadlock."""
        t0 = time.monotonic()
        _batch = (sla_class == "batch" and self._sem_batch is not None)
        with self._lock:
            self.waiting += 1
        _got_batch = False
        try:
            if _batch:
                if not self._sem_batch.acquire(timeout=max(0.0, float(deadline_s))):
                    raise DispatchTimeout(
                        f"waited {time.monotonic() - t0:.0f}s for a '{self.key}' BATCH sub-slot "
                        f"(batch limit {self.limit - self.reserve} of {self.limit}; {self.reserve} reserved for "
                        f"realtime) — deadline {float(deadline_s):.0f}s exhausted")
                _got_batch = True
            remaining = float(deadline_s) - (time.monotonic() - t0)
            if not self._sem.acquire(timeout=max(0.0, remaining)):
                raise DispatchTimeout(
                    f"waited {time.monotonic() - t0:.0f}s for a '{self.key}' dispatch slot "
                    f"(limit {self.limit}, all in flight) — deadline {float(deadline_s):.0f}s exhausted")
        except BaseException:
            if _got_batch:                              # took the batch sub-slot but not the main slot → hand it back
                try:
                    self._sem_batch.release()
                except ValueError:
                    pass
            raise
        finally:
            with self._lock:
                self.waiting -= 1
        # Concurrency slot held. Now pace by RPM, still inside the deadline; on timeout, hand BOTH slots back.
        while True:
            wait = self._rpm_wait_s()
            if wait <= 0:
                break
            remaining = float(deadline_s) - (time.monotonic() - t0)
            if remaining <= 0:
                self._sem.release()
                if _batch:
                    try:
                        self._sem_batch.release()
                    except ValueError:
                        pass
                raise DispatchTimeout(
                    f"'{self.key}' rate limit ({self.rpm}/min): no token within deadline "
                    f"{float(deadline_s):.0f}s")
            time.sleep(min(wait, remaining))
        # Then pace by TPM (this call's estimated input+output tokens), still inside the deadline — the axis that
        # actually 429s. Same hand-both-slots-back-on-timeout contract as the RPM loop above.
        while True:
            wait = self._tpm_wait_s(est_tokens)
            if wait <= 0:
                break
            remaining = float(deadline_s) - (time.monotonic() - t0)
            if remaining <= 0:
                self._sem.release()
                if _batch:
                    try:
                        self._sem_batch.release()
                    except ValueError:
                        pass
                raise DispatchTimeout(
                    f"'{self.key}' token rate limit ({self.tpm}/min): {int(est_tokens)} est tokens did not fit "
                    f"within deadline {float(deadline_s):.0f}s")
            time.sleep(min(wait, remaining))
        with self._lock:
            self.in_flight += 1
        return time.monotonic() - t0

    def release(self, sla_class=None):
        with self._lock:
            if self.in_flight > 0:
                self.in_flight -= 1
        try:
            self._sem.release()
        except ValueError:
            # BoundedSemaphore over-release: means release() was called without a matching acquire (a caller
            # bug). Swallowing keeps the governor from crashing a call path, and in_flight already stayed sane.
            pass
        if sla_class == "batch" and self._sem_batch is not None:
            try:
                self._sem_batch.release()               # free the batch sub-slot too (pair the sla_class the acquire used)
            except ValueError:
                pass


class Governor:
    """The process-wide set of per-key buckets. One instance (`_GOV`); callers use the module functions."""

    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()
        self._global = None

    def _global_sem(self):
        if self._global is None:
            with self._lock:
                if self._global is None:
                    self._global = threading.BoundedSemaphore(max(1, _limit("global_concurrency",
                                                                          DEFAULT_GLOBAL_CONCURRENCY)))
        return self._global

    def _key_and_limit(self, vendor, model, skip_lane=False):
        """(key, concurrency_limit, rpm, is_lane) for this call. Lane vendors collapse to one key + the lane
        budget; metered vendors key by vendor with the vendor budget. `is_lane` is the AUTHORITATIVE signal
        from adapters._lane_for (whether this vendor rides an active subscription lane) — returned as data so
        no caller has to re-derive lane-ness by parsing the key string. Config can override either limit.

        `skip_lane=True` forces the VENDOR key even for a vendor that rides a lane: the caller is deliberately
        taking the METERED path (a lane→metered shed after the lane's own bucket saturated), so it must be gated
        on the metered vendor cap — never re-queued behind the very lane bucket it just timed out of."""
        vendor = (vendor or "").strip().lower()
        lane = None
        if not skip_lane:
            try:
                from . import adapters
                got = adapters._lane_for(vendor)
                lane = got[0] if got else None
            except Exception:
                lane = None
        if lane:
            key = f"lane:{lane}"
            # PER-LANE override → the global lane cap → the default: dispatch.lane_concurrency_<lane> (e.g.
            # lane_concurrency_codex=10) lets a lane that parallelises well run near its own plan ceiling, while a
            # process-bound lane (a cold CLI) can be set lower — one cap no longer forces every plan to the minimum.
            limit = _limit(f"lane_concurrency_{lane}", _limit("lane_concurrency", DEFAULT_LANE_CONCURRENCY))
        else:
            key = f"vendor:{vendor}"
            # PER-VENDOR override → the global metered cap → the default: dispatch.vendor_concurrency_<vendor> (e.g.
            # vendor_concurrency_openai=12) tunes each provider to ITS real rate limit — never one hardcoded number for
            # every vendor. Mirrors the per-lane override; this cap is now also enforced ACROSS processes (acquire()).
            limit = _limit(f"vendor_concurrency_{vendor}", _limit("vendor_concurrency", DEFAULT_VENDOR_CONCURRENCY))
        rpm = _limit(f"rpm_{vendor}", DEFAULT_RPM)     # per-vendor RPM, e.g. SPENDGUARD_DISPATCH_RPM_MOONSHOT=60
        if not rpm and not lane:                       # no explicit rpm on a METERED vendor → use the SELF-LEARNED limit
            rpm = int((_LEARNED.for_vendor(vendor) or {}).get("rpm") or 0)   # from a 429 header (learn_rate_limit); 0 until learned
        return key, limit, rpm, bool(lane)

    def _reserve_for(self, key, limit):
        """Realtime-reserved slots for this key: dispatch.realtime_reserve_<lane|vendor> → dispatch.realtime_reserve
        → 0 (off). Capped to the _Bucket invariant (≤ limit-1) HERE too, so the re-key comparison below matches the
        value __init__ actually stores and never re-creates the bucket on every call."""
        short = key.split(":", 1)[-1]
        raw = _limit(f"realtime_reserve_{short}", _limit("realtime_reserve", 0))
        return max(0, min(int(raw or 0), max(1, int(limit)) - 1))

    def _bucket(self, vendor, model, skip_lane=False):
        key, limit, rpm, _is_lane = self._key_and_limit(vendor, model, skip_lane=skip_lane)
        reserve = self._reserve_for(key, limit)
        # TPM pacing is a METERED-vendor ceiling — a $0 subscription LANE (CLI) has no tokens/minute 429, and its key
        # is shared across vendors, so a per-vendor tpm would thrash one lane bucket. So: 0 for a lane key, else the
        # per-vendor `tpm_<vendor>` (keyed on the already-normalised vendor in the key, matching _reserve_for).
        _vk = key.split(":", 1)[-1]                     # the normalised vendor for a vendor: key
        tpm = 0 if _is_lane else (_limit(f"tpm_{_vk}", DEFAULT_TPM)                 # explicit config >
                                  or int((_LEARNED.for_vendor(_vk) or {}).get("tpm") or 0))   # SELF-LEARNED (429 header) > 0
        with self._lock:
            b = self._buckets.get(key)
            # Re-key if the configured limit/rpm/tpm/reserve changed since the bucket was made (config edited at
            # runtime): a stale semaphore size or bucket rate would silently ignore the new limit, the exact
            # "measurement looked up under the wrong key" failure this project keeps hitting.
            if (b is None or b.limit != max(1, int(limit)) or b.rpm != max(0, int(rpm))
                    or b.reserve != reserve or b.tpm != max(0, int(tpm))):
                b = _Bucket(key, limit, rpm, reserve, tpm)
                self._buckets[key] = b
            return b

    def acquire(self, vendor, model, deadline_s, skip_lane=False, sla_class=None, est_tokens=0):
        """Admit one call. Returns seconds waited (0 when uncontended). Raises DispatchTimeout on deadline.
        Order: global slot → per-key in-process slot → cross-process slot (lane keys AND metered vendor keys — a
        per-provider cap shared across processes). release() unwinds all three; the held cross-process slot rides a
        per-thread stack that release() pops (acquire and release run on the same fan_out worker thread).
        `skip_lane=True` gates on the metered VENDOR key even for a lane vendor (a lane→metered shed); the paired
        release() MUST pass the same skip_lane so it frees the same bucket. `sla_class="batch"` gates on the batch
        SUB-limit of the per-key bucket (when a realtime reserve is configured for that key), so a batch fan can't
        starve realtime of a slot; the paired release() MUST pass the same sla_class. The reserve is IN-PROCESS
        (per-key bucket) only — the global ceiling and the cross-process cap are coarse backstops, not reserved."""
        if _off() or not deadline_s or float(deadline_s) <= 0:
            _held().append(None)                     # keep the acquire/release stack balanced even as a no-op
            return 0.0
        t0 = time.monotonic()
        # REACTIVE COOL: a recent 429 on this vendor (its Retry-After, via learn_rate_limit) holds new admissions off
        # until it clears — bounded by the caller's deadline (a cool longer than the deadline is an honest DispatchTimeout,
        # 'could not run in time', not a silent 429 retry). Held BEFORE any slot, so a cooling vendor's calls wait idle,
        # not holding the global/bucket slots. No _held append yet, so nothing to unwind on this raise.
        _cl = _cooldown_left(vendor)
        if _cl > 0:
            if _cl >= float(deadline_s):
                raise DispatchTimeout(f"'{vendor}' cooling {_cl:.0f}s after a 429 (Retry-After) — deadline "
                                      f"{float(deadline_s):.0f}s exhausted")
            time.sleep(_cl)
        g = self._global_sem()
        if not g.acquire(timeout=max(0.0, float(deadline_s) - (time.monotonic() - t0))):
            raise DispatchTimeout(f"machine-wide dispatch ceiling ({_limit('global_concurrency', DEFAULT_GLOBAL_CONCURRENCY)}) "
                                  f"full — deadline {float(deadline_s):.0f}s exhausted")
        got_bucket, xp = False, None
        try:
            key, limit, _rpm, _is_lane = self._key_and_limit(vendor, model, skip_lane=skip_lane)
            self._bucket(vendor, model, skip_lane=skip_lane).acquire(
                float(deadline_s) - (time.monotonic() - t0), sla_class=sla_class, est_tokens=est_tokens)
            got_bucket = True
            if not _xp_off():                        # co-govern ACROSS processes: a lane's shared subscription plan AND
                # a metered vendor's per-provider cap. Previously lane-only — so N concurrent runs each ran up to
                # vendor_concurrency to ONE metered provider (8 in-process × N procs) and 429-STORMED it. Now they share
                # ONE cross-process cap (`limit` slots, per-vendor tunable) and self-throttle instead. _acquire_xp is
                # key-generic (lane:/vendor: alike). SPENDGUARD_DISPATCH_XP_OFF=1 disables this whole cross-process layer.
                xp = _acquire_xp(key, limit, float(deadline_s) - (time.monotonic() - t0))
        except BaseException:                        # unwind anything already taken, in reverse, then re-raise
            if xp is not None:
                xp.release()
            if got_bucket:
                self._bucket(vendor, model, skip_lane=skip_lane).release(sla_class=sla_class)
            g.release()
            raise
        _held().append(xp)
        return time.monotonic() - t0

    def release(self, vendor, model, skip_lane=False, sla_class=None):
        xp = _pop_held()                             # cross-process slot first (or None), always
        if xp is not None:
            try:
                xp.release()
            except Exception:
                pass
        if _off():
            return
        try:
            self._bucket(vendor, model, skip_lane=skip_lane).release(sla_class=sla_class)
        finally:
            try:
                if self._global is not None:
                    self._global.release()
            except ValueError:
                pass

_GOV = Governor()


def acquire(vendor, model, deadline_s, skip_lane=False, sla_class=None, est_tokens=0):
    """Admit one dispatch to (vendor, model), blocking up to deadline_s. Returns seconds waited. Raises
    DispatchTimeout if no slot frees in time. Pair with release() in a finally. `skip_lane=True` gates on the
    metered VENDOR cap even for a lane vendor (the lane→metered shed); release() must be given the same flag.
    `sla_class="batch"` admits against the batch SUB-limit so a batch fan can't take a realtime-reserved slot (only
    when dispatch.realtime_reserve[_<key>] is set for that key; off by default); release() must pass the same value.
    `est_tokens` (this call's estimated input+output) paces against the per-vendor TPM ceiling — 0 (the default) or an
    unconfigured tpm means no token pacing, so every existing caller is unchanged."""
    return _GOV.acquire(vendor, model, deadline_s, skip_lane=skip_lane, sla_class=sla_class, est_tokens=est_tokens)


def acquire_or_none(vendor, model, deadline_s, skip_lane=False, sla_class=None, est_tokens=0):
    """acquire(), except a queue-slot TIMEOUT returns None instead of raising DispatchTimeout — everything else about
    it (full deadline, same key, the paired release()) is identical. This is the admission primitive a BULK FAN-OUT
    needs: with hundreds of tasks sharing one lane's slots, a saturated slot on ONE task must become THAT task's
    per-task MISS (contained, then batched/queued/retried), and MUST NOT raise out of its worker to abort the batch
    or crash the caller — the measured failure was a saturated kimi-code lane raising DispatchTimeout out of a
    532-task review fan and killing the whole honestreview process. Only the deadline TIMEOUT is converted; a genuine
    SPEND REFUSAL is a different type and is not caught here (acquire does not raise one anyway). Callers pair it with
    release() in a finally exactly as acquire(); None means 'no slot — do not release, this task did not run'."""
    try:
        return acquire(vendor, model, deadline_s, skip_lane=skip_lane, sla_class=sla_class, est_tokens=est_tokens)
    except DispatchTimeout:
        return None


def release(vendor, model, skip_lane=False, sla_class=None):
    """Return the dispatch slot acquired for (vendor, model). Safe to call once per successful acquire. Pass the
    SAME skip_lane AND sla_class the paired acquire() used, so the freed bucket/sub-slot is the one that was taken."""
    _GOV.release(vendor, model, skip_lane=skip_lane, sla_class=sla_class)


def holding():
    """True when THIS thread is already inside an acquire()d dispatch slot (governed / bulk_delegate already admitted
    this logical call). The universal-admission path in adapters.call reads it so a call that is ALREADY governed by an
    outer acquire never takes a SECOND slot — one admission per logical call, exactly as _route_guard is one record per
    logical call. Every acquire() (even the OFF/no-deadline no-op) pushes the per-thread stack, so this is authoritative
    for 'am I under the governor right now' without re-deriving it from vendor/model."""
    return bool(_held())


# ── GOVERNED ADMISSION WITH SHED-TO-METERED — the ONE owner of that policy ────────────────────────────────────────
# So vendor_call.call and adapters.call(governed=True) SHARE it and can never drift (the exact 'one brain, two
# entries' the capability map is about). A lane vendor SPLITS its deadline (LANE_QUEUE_SHARE): the lane may queue for
# up to this share, and the rest is RESERVED so a saturated-lane shed to the metered twin can still complete.
LANE_QUEUE_SHARE = 0.5


class _Admission:
    """The outcome of admit(): `.ok` (proceed) · `.shed` (run the call metered_only — a saturated $0 lane shed to
    its metered twin) · `.held` (a governor slot is held; release() frees it) · `.error` (the deadline reason when
    not .ok). .release() is idempotent and a no-op when nothing is held (governor OFF / unavailable)."""
    __slots__ = ("ok", "shed", "held", "error", "_vendor", "_model", "_skip_lane", "_released")

    def __init__(self, ok, shed, held, vendor, model, skip_lane, error=None):
        self.ok, self.shed, self.held, self.error = ok, shed, held, error
        self._vendor, self._model, self._skip_lane, self._released = vendor, model, skip_lane, False

    def release(self):
        if self.held and not self._released:
            self._released = True
            try:
                release(self._vendor, self._model, skip_lane=self._skip_lane)
            except Exception:
                pass


def admit(vendor, model, deadline_s, no_metered_fallback=False, est_tokens=0, shed=True, skip_lane=False):
    """Governed admission for (vendor, model) with the SHED-TO-METERED policy. For a $0 LANE vendor the deadline is
    SPLIT (LANE_QUEUE_SHARE) so a saturated lane's queue wait can't starve the shed; on a lane queue-timeout it
    re-acquires under the metered VENDOR key and returns .shed=True (the caller runs metered_only) — lane-first,
    never a hard fail. A saturated METERED vendor (no cheaper twin) or no_metered_fallback → .ok=False (the caller
    returns a DEADLINE result). Governor OFF/unavailable → proceed UNGOVERNED (.ok, not held), LOUDLY — the gate is
    the real $ backstop and dispatch's kill-switch philosophy is that spend discipline must not depend on the
    scheduler being correct. Returns an _Admission (.ok/.shed/.held/.release()).

    `shed=False` DISABLES the lane→metered shed: a saturated $0 lane just queues out (.ok=False) rather than moving the
    call onto the paid API. It is for a MANAGED SERIAL call (universal admission), where silently paying to escape a
    busy lane would be surprise spend on a plain adapters.call — a fan WANTS the shed (throughput), a lone call does
    not. With shed=False the lane vendor also gets the FULL deadline (no reserve is held back for a shed that can't
    happen). `skip_lane=True` gates on the metered VENDOR bucket even for a vendor that rides a lane — for a
    metered_only call, which hits the paid API and so must be paced by that vendor's RPM/TPM, not the $0 lane's
    (TPM-less) concurrency bucket."""
    if not deadline_s or float(deadline_s) <= 0:
        return _Admission(True, False, False, vendor, model, False)
    try:
        from . import adapters
        rode_lane = (not skip_lane) and bool(adapters._lane_for(vendor))   # skip_lane=True (a metered_only call) →
    except Exception:                                                      # gate on the metered VENDOR bucket (with
        rode_lane = False                                                  # its RPM/TPM), never the $0 lane bucket
    t0 = time.monotonic()
    lane_deadline = (float(deadline_s) * LANE_QUEUE_SHARE) if (rode_lane and shed) else float(deadline_s)
    try:
        acquire(vendor, model, lane_deadline, skip_lane=skip_lane, est_tokens=est_tokens)
        return _Admission(True, False, True, vendor, model, skip_lane=skip_lane)
    except DispatchTimeout as dt:
        left = float(deadline_s) - (time.monotonic() - t0)
        if shed and rode_lane and not no_metered_fallback and left > 0:
            try:
                acquire(vendor, model, left, skip_lane=True, est_tokens=est_tokens)   # gate on the metered vendor cap, not the saturated lane
                return _Admission(True, True, True, vendor, model, skip_lane=True)
            except DispatchTimeout as dt2:
                return _Admission(False, False, False, vendor, model, False, error=str(dt2))
        return _Admission(False, False, False, vendor, model, False, error=str(dt))
    except Exception as _ge:
        # The GOVERNOR ITSELF is unavailable (import/infra/config), NOT a queue timeout. Degrade LOUDLY to an
        # ungoverned admission rather than block real work: the spend GATE is the real $ backstop, and this module's
        # own kill-switch (SPENDGUARD_DISPATCH_OFF) exists precisely because spend discipline must never depend on the
        # scheduler being correct. Loud (not silent) so a broken governor is visible and can be fixed.
        import sys as _sys
        print(f"[spendguard] dispatch governor unavailable ({type(_ge).__name__}: {str(_ge)[:80]}) — admitting "
              f"{vendor}/{model} UNGOVERNED this call; the spend gate still enforces the cap", file=_sys.stderr)
        return _Admission(True, False, False, vendor, model, False)


def queue_state():
    """Current per-key admission state — {key: {limit, rpm, in_flight, waiting}}. Named uniquely (not `stats`)
    so it never collides with semcache.stats, an unrelated job (NAME_REGISTRY). What a receipt/doctor shows to
    answer 'is anything queued right now'."""
    with _GOV._lock:
        return {k: {"limit": b.limit, "rpm": b.rpm, "tpm": b.tpm, "in_flight": b.in_flight, "waiting": b.waiting}
                for k, b in _GOV._buckets.items()}


def lane_free(lane):
    """Free concurrency slots on a subscription LANE right now = limit − in_flight − waiting. The signal a fan uses
    for LEAST-LOADED dispatch: pick the arm with the MOST free slots so no lane idles while a slow one bottlenecks,
    and a momentarily-slow lane stops attracting new work within the run. A pure READ — never creates a bucket; a
    lane with no bucket yet is FULLY free (its whole cap), so the first tasks fill every empty lane first, which is
    exactly what seeds the cross-vendor SPREAD before the fast lanes start pulling the overflow."""
    key = f"lane:{lane}"
    limit = _limit(f"lane_concurrency_{lane}", _limit("lane_concurrency", DEFAULT_LANE_CONCURRENCY))
    with _GOV._lock:
        b = _GOV._buckets.get(key)
        if b is None:
            return max(1, int(limit))
        return max(0, b.limit - b.in_flight - b.waiting)


# ── ADMISSION CONTROL (the non-blocking axis acquire() lacks) ────────────────────────────────────────────────────
# acquire() BLOCKS up to a deadline, so a saturated machine leaves callers spawned-and-WAITING — hundreds of hook
# processes each holding RAM while queued. try_admit() is the missing primitive: a caller checks for a slot and, if
# none is free, SHEDS immediately (never spawns / never waits). Machine-wide (flock), so one shared ceiling bounds
# the whole hook fleet (honestreview / ccwatch / 7thsense) across every process, not per-system.
DEFAULT_ADMIT = 6              # concurrent heavy hook-ops machine-wide; override via SPENDGUARD_DISPATCH_ADMIT_<POOL> / config


class _NoopAdmit:
    """The handle try_admit() returns when cross-process gating is OFF (no fcntl, or XP_OFF) — admits everyone so a
    non-POSIX host is never blocked; .release() is a no-op."""
    __slots__ = ()

    def release(self):
        pass


_NOOP_ADMIT = _NoopAdmit()


def _admit_limit(pool):
    """Machine-wide ceiling for a named admission POOL — env SPENDGUARD_DISPATCH_ADMIT_<POOL> wins, then config
    dispatch.admit_<pool>, else DEFAULT_ADMIT. One number the whole fleet shares."""
    return _limit(f"admit_{pool}", DEFAULT_ADMIT)


def try_admit(pool="hooks", limit=None):
    """NON-BLOCKING cross-process admission on a named POOL. Reserve one of `limit` machine-wide slots for `pool`
    RIGHT NOW and return a handle (truthy; call `.release()` when done), or None if every slot is held (SHED — do NOT
    spawn, do NOT wait). This is what lets a hook fleet self-limit at the SOURCE: honestreview/ccwatch/7thsense call
    `h = dispatch.try_admit('hooks')` and skip-with-a-note when `h is None`, so a busy machine never accumulates
    hundreds of waiting review processes — a refused caller never starts. Slots are flock-based, so a crashed
    holder's slot frees automatically (no stale reap). Gating-off / no fcntl → a no-op handle that always admits."""
    lim = int(limit) if limit is not None else _admit_limit(pool)
    if _off() or _xp_off():
        return _NOOP_ADMIT
    try:
        return _acquire_xp(f"admit:{pool}", max(1, lim), 0.0)   # deadline 0 → one non-blocking pass, then shed
    except DispatchTimeout:
        return None
