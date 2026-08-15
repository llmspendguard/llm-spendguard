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
DEFAULT_LANE_CONCURRENCY = 3       # max concurrent calls sharing one subscription lane (claude-code/codex/zai)
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

    def _key_and_limit(self, vendor, model):
        """(key, concurrency_limit, rpm) for this call. Lane vendors collapse to one key + the lane budget;
        metered vendors key by vendor with the vendor budget. Config can override either limit."""
        vendor = (vendor or "").strip().lower()
        lane = None
        try:
            from . import adapters
            got = adapters._lane_for(vendor)
            lane = got[0] if got else None
        except Exception:
            lane = None
        if lane:
            key = f"lane:{lane}"
            limit = _limit("lane_concurrency", DEFAULT_LANE_CONCURRENCY)
        else:
            key = f"vendor:{vendor}"
            limit = _limit("vendor_concurrency", DEFAULT_VENDOR_CONCURRENCY)
        rpm = _limit(f"rpm_{vendor}", DEFAULT_RPM)     # per-vendor RPM, e.g. SPENDGUARD_DISPATCH_RPM_MOONSHOT=60
        return key, limit, rpm

    def _bucket(self, vendor, model):
        key, limit, rpm = self._key_and_limit(vendor, model)
        with self._lock:
            b = self._buckets.get(key)
            # Re-key if the configured limit/rpm changed since the bucket was made (config edited at runtime):
            # a stale semaphore size would silently ignore the new limit, the exact "measurement looked up under
            # the wrong key" failure this project keeps hitting.
            if b is None or b.limit != max(1, int(limit)) or b.rpm != max(0, int(rpm)):
                b = _Bucket(key, limit, rpm)
                self._buckets[key] = b
            return b

    def acquire(self, vendor, model, deadline_s):
        """Admit one call. Returns seconds waited (0 when uncontended). Raises DispatchTimeout on deadline.
        Acquires the GLOBAL slot first, then the per-key slot — released in reverse by release()."""
        if _off() or not deadline_s or float(deadline_s) <= 0:
            return 0.0
        t0 = time.monotonic()
        g = self._global_sem()
        if not g.acquire(timeout=max(0.0, float(deadline_s))):
            raise DispatchTimeout(f"machine-wide dispatch ceiling ({_limit('global_concurrency', DEFAULT_GLOBAL_CONCURRENCY)}) "
                                  f"full — deadline {float(deadline_s):.0f}s exhausted")
        try:
            remaining = float(deadline_s) - (time.monotonic() - t0)
            waited = self._bucket(vendor, model).acquire(remaining)
            return waited + (time.monotonic() - t0 - waited)
        except BaseException:
            g.release()                              # never leak the global slot if the per-key acquire fails
            raise

    def release(self, vendor, model):
        if _off():
            return
        try:
            self._bucket(vendor, model).release()
        finally:
            try:
                if self._global is not None:
                    self._global.release()
            except ValueError:
                pass

_GOV = Governor()


def acquire(vendor, model, deadline_s):
    """Admit one dispatch to (vendor, model), blocking up to deadline_s. Returns seconds waited. Raises
    DispatchTimeout if no slot frees in time. Pair with release() in a finally."""
    return _GOV.acquire(vendor, model, deadline_s)


def release(vendor, model):
    """Return the dispatch slot acquired for (vendor, model). Safe to call once per successful acquire."""
    _GOV.release(vendor, model)


def queue_state():
    """Current per-key admission state — {key: {limit, rpm, in_flight, waiting}}. Named uniquely (not `stats`)
    so it never collides with semcache.stats, an unrelated job (NAME_REGISTRY). What a receipt/doctor shows to
    answer 'is anything queued right now'."""
    with _GOV._lock:
        return {k: {"limit": b.limit, "rpm": b.rpm, "in_flight": b.in_flight, "waiting": b.waiting}
                for k, b in _GOV._buckets.items()}
