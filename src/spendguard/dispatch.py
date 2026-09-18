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
    """One key's admission state: a bounded-concurrency semaphore + an optional requests/minute token bucket.
    Thread-safe. The bucket refills continuously from monotonic time — no background thread, no cron."""

    __slots__ = ("key", "limit", "rpm", "_sem", "_lock", "_tokens", "_last", "in_flight", "waiting")

    def __init__(self, key, limit, rpm):
        self.key = key
        self.limit = max(1, int(limit))
        self.rpm = max(0, int(rpm))
        self._sem = threading.BoundedSemaphore(self.limit)
        self._lock = threading.Lock()
        self._tokens = float(self.rpm)     # start full so the first burst up to rpm is not paced
        self._last = time.monotonic()
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

    def acquire(self, deadline_s):
        """Block until a concurrency slot is free AND (if paced) an RPM token is available, within deadline_s.
        Returns seconds waited. Raises DispatchTimeout if the deadline passes first."""
        t0 = time.monotonic()
        with self._lock:
            self.waiting += 1
        try:
            if not self._sem.acquire(timeout=max(0.0, float(deadline_s))):
                raise DispatchTimeout(
                    f"waited {time.monotonic() - t0:.0f}s for a '{self.key}' dispatch slot "
                    f"(limit {self.limit}, all in flight) — deadline {float(deadline_s):.0f}s exhausted")
        finally:
            with self._lock:
                self.waiting -= 1
        # Concurrency slot held. Now pace by RPM, still inside the deadline; on timeout, hand the slot back.
        while True:
            wait = self._rpm_wait_s()
            if wait <= 0:
                break
            remaining = float(deadline_s) - (time.monotonic() - t0)
            if remaining <= 0:
                self._sem.release()
                raise DispatchTimeout(
                    f"'{self.key}' rate limit ({self.rpm}/min): no token within deadline "
                    f"{float(deadline_s):.0f}s")
            time.sleep(min(wait, remaining))
        with self._lock:
            self.in_flight += 1
        return time.monotonic() - t0

    def release(self):
        with self._lock:
            if self.in_flight > 0:
                self.in_flight -= 1
        try:
            self._sem.release()
        except ValueError:
            # BoundedSemaphore over-release: means release() was called without a matching acquire (a caller
            # bug). Swallowing keeps the governor from crashing a call path, and in_flight already stayed sane.
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
        return key, limit, rpm, bool(lane)

    def _bucket(self, vendor, model, skip_lane=False):
        key, limit, rpm, _is_lane = self._key_and_limit(vendor, model, skip_lane=skip_lane)
        with self._lock:
            b = self._buckets.get(key)
            # Re-key if the configured limit/rpm changed since the bucket was made (config edited at runtime):
            # a stale semaphore size would silently ignore the new limit, the exact "measurement looked up under
            # the wrong key" failure this project keeps hitting.
            if b is None or b.limit != max(1, int(limit)) or b.rpm != max(0, int(rpm)):
                b = _Bucket(key, limit, rpm)
                self._buckets[key] = b
            return b

    def acquire(self, vendor, model, deadline_s, skip_lane=False):
        """Admit one call. Returns seconds waited (0 when uncontended). Raises DispatchTimeout on deadline.
        Order: global slot → per-key in-process slot → cross-process slot (lane keys AND metered vendor keys — a
        per-provider cap shared across processes). release() unwinds all three; the held cross-process slot rides a
        per-thread stack that release() pops (acquire and release run on the same fan_out worker thread).
        `skip_lane=True` gates on the metered VENDOR key even for a lane vendor (a lane→metered shed); the paired
        release() MUST pass the same skip_lane so it frees the same bucket."""
        if _off() or not deadline_s or float(deadline_s) <= 0:
            _held().append(None)                     # keep the acquire/release stack balanced even as a no-op
            return 0.0
        t0 = time.monotonic()
        g = self._global_sem()
        if not g.acquire(timeout=max(0.0, float(deadline_s))):
            raise DispatchTimeout(f"machine-wide dispatch ceiling ({_limit('global_concurrency', DEFAULT_GLOBAL_CONCURRENCY)}) "
                                  f"full — deadline {float(deadline_s):.0f}s exhausted")
        got_bucket, xp = False, None
        try:
            key, limit, _rpm, _is_lane = self._key_and_limit(vendor, model, skip_lane=skip_lane)
            self._bucket(vendor, model, skip_lane=skip_lane).acquire(float(deadline_s) - (time.monotonic() - t0))
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
                self._bucket(vendor, model, skip_lane=skip_lane).release()
            g.release()
            raise
        _held().append(xp)
        return time.monotonic() - t0

    def release(self, vendor, model, skip_lane=False):
        xp = _pop_held()                             # cross-process slot first (or None), always
        if xp is not None:
            try:
                xp.release()
            except Exception:
                pass
        if _off():
            return
        try:
            self._bucket(vendor, model, skip_lane=skip_lane).release()
        finally:
            try:
                if self._global is not None:
                    self._global.release()
            except ValueError:
                pass

_GOV = Governor()


def acquire(vendor, model, deadline_s, skip_lane=False):
    """Admit one dispatch to (vendor, model), blocking up to deadline_s. Returns seconds waited. Raises
    DispatchTimeout if no slot frees in time. Pair with release() in a finally. `skip_lane=True` gates on the
    metered VENDOR cap even for a lane vendor (the lane→metered shed); release() must be given the same flag."""
    return _GOV.acquire(vendor, model, deadline_s, skip_lane=skip_lane)


def release(vendor, model, skip_lane=False):
    """Return the dispatch slot acquired for (vendor, model). Safe to call once per successful acquire. Pass the
    SAME skip_lane the paired acquire() used, so the freed bucket is the one that was taken."""
    _GOV.release(vendor, model, skip_lane=skip_lane)


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


def admit(vendor, model, deadline_s, no_metered_fallback=False):
    """Governed admission for (vendor, model) with the SHED-TO-METERED policy. For a $0 LANE vendor the deadline is
    SPLIT (LANE_QUEUE_SHARE) so a saturated lane's queue wait can't starve the shed; on a lane queue-timeout it
    re-acquires under the metered VENDOR key and returns .shed=True (the caller runs metered_only) — lane-first,
    never a hard fail. A saturated METERED vendor (no cheaper twin) or no_metered_fallback → .ok=False (the caller
    returns a DEADLINE result). Governor OFF/unavailable → proceed UNGOVERNED (.ok, not held), LOUDLY — the gate is
    the real $ backstop and dispatch's kill-switch philosophy is that spend discipline must not depend on the
    scheduler being correct. Returns an _Admission (.ok/.shed/.held/.release())."""
    if not deadline_s or float(deadline_s) <= 0:
        return _Admission(True, False, False, vendor, model, False)
    try:
        from . import adapters
        rode_lane = bool(adapters._lane_for(vendor))
    except Exception:
        rode_lane = False
    t0 = time.monotonic()
    lane_deadline = (float(deadline_s) * LANE_QUEUE_SHARE) if rode_lane else float(deadline_s)
    try:
        acquire(vendor, model, lane_deadline)
        return _Admission(True, False, True, vendor, model, skip_lane=False)
    except DispatchTimeout as dt:
        left = float(deadline_s) - (time.monotonic() - t0)
        if rode_lane and not no_metered_fallback and left > 0:
            try:
                acquire(vendor, model, left, skip_lane=True)   # gate on the metered vendor cap, not the saturated lane
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
        return {k: {"limit": b.limit, "rpm": b.rpm, "in_flight": b.in_flight, "waiting": b.waiting}
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
