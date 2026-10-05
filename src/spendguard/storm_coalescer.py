"""Submission-side STORM COALESCER — the one place concurrent LLM submissions converge so a burst is planned as a
COHORT instead of stampeding the realtime rate wall one call at a time.

ROOT CAUSE this exists for (docs/PLAN_429_storm_to_batch.md): the per-vendor RPM `_Bucket` STARTS FULL, so a burst
≤ rpm is admitted entirely UNPACED — per-request admission structurally cannot catch a storm (the burst is admitted
before pacing engages; at incident scale that produced 8,088 × 429). The fix is NOT smarter per-request admission; it
is to stop deciding per request: accumulate arrivals into a cohort cut at an adaptive LOGICAL BREAK, then route the
cohort as the COMBO — pace the share realtime can SUSTAIN within the urgency horizon, divert the OVERFLOW to the Batch
API — and resolve each submission's future from whichever path served it. Async underneath; the caller just awaits its
future. No raw 429 ever surfaces (a realtime 429 is re-routed to batch).

PURITY / INJECTION: execution is injected so this module is timing+routing only and is testable offline against
`tests/storm_harness.FakeProvider`:
  execute_realtime(req) -> result dict                       # production: an admit-governed adapters.call; here: the fake wall
  execute_batch(list[(custom_id, req)]) -> {custom_id: result}  # ID-KEYED and UNORDERED: real Batch APIs return by id
                                                             #   and may omit a failed id. production: provider-aware
                                                             #   (openai→submit.submit_chat_tasks; anthropic→Message-
                                                             #   Batch path) submit+collect; here: the fake batch.
Both return adapters-shaped dicts. `execute_batch` is demuxed BY ID (never positionally — a positional zip corrupts
demux when the provider drops/reorders, and the Prompt 9 panel caught exactly that bug). It MAY block (a real Batch
API takes minutes); it runs on a dedicated batch worker so it never stalls the planner or the realtime pacer. A result
dict carrying status_code/http_status 429 or 529, or an `error`, is a per-item failure and is ISOLATED (only that id is
refilled; siblings are untouched). A result dict carrying status_code/http_status 429 or 529 is treated as rate-limited.

MECHANISMS OPENED (Prompt 8): the bucket starts full (→ own pacer, I5); submit_chat_tasks is OpenAI-only (→ injected
provider-aware batch seam); plan_batch_chunks returns int counts (→ slice overflow by count, never drop the tail, I14).
"""
import math
import threading
import time
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor

from . import queue_planner


def _is_rate_limited(r):
    """True iff a result dict is a rate-limit outcome. PARSES a known field (status), never judges meaning."""
    if not isinstance(r, dict):
        return False
    return (r.get("status_code") or r.get("http_status")) in (429, 529)


class _Pending:
    """One submitted request + the future the caller awaits. `seq` preserves submission identity for demux/order."""
    __slots__ = ("req", "future", "seq")

    def __init__(self, req, seq):
        self.req = req
        self.future = Future()
        self.seq = seq


class _Pacer:
    """A SHARED monotonic rate pacer: hands out release times ≥ 1/rate apart so the aggregate realtime egress across
    ALL concurrent cohorts stays ≤ the sustainable rate. Unlike the RPM `_Bucket` it does NOT start full — a storm must
    be paced from the first call, which is the whole point."""

    def __init__(self, rate_per_s):
        self._gap = 1.0 / float(rate_per_s)
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            due = self._next if self._next > now else now
            self._next = due + self._gap
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class StormCoalescer:
    """Accumulate concurrent submissions into cohorts cut at an adaptive LOGICAL BREAK, then route each cohort as the
    COMBO (pace sustainable realtime + batch the overflow), resolving every future exactly once. Execution injected."""

    def __init__(self, *, execute_realtime, execute_batch, sustainable_rate_per_s, horizon_s,
                 idle_gap_s=0.01, max_wait_s=1.0, realtime_concurrency=8, route_concurrency=4,
                 batch_concurrency=4, provider="?", batch_refill_depth=1):
        if not callable(execute_realtime) or not callable(execute_batch):
            raise ValueError("execute_realtime and execute_batch are REQUIRED callables (no silent default)")
        if not (sustainable_rate_per_s > 0 and math.isfinite(sustainable_rate_per_s)):
            raise ValueError("sustainable_rate_per_s must be a FINITE number > 0 (rpm/60 for the vendor); got %r "
                             "(inf/NaN would silently disable pacing)" % (sustainable_rate_per_s,))
        if not (horizon_s > 0 and math.isfinite(horizon_s)):
            raise ValueError("horizon_s must be a finite number > 0; got %r (<=0 collapses the realtime budget to 1)"
                             % (horizon_s,))
        if idle_gap_s <= 0 or max_wait_s <= 0:
            raise ValueError("idle_gap_s and max_wait_s must be > 0; got idle_gap_s=%r max_wait_s=%r"
                             % (idle_gap_s, max_wait_s))
        self._execute_realtime = execute_realtime
        self._execute_batch = execute_batch
        self.sustainable_rate_per_s = float(sustainable_rate_per_s)
        self.horizon_s = float(horizon_s)
        self.idle_gap_s = float(idle_gap_s)
        self.max_wait_s = float(max_wait_s)
        self.provider = provider
        self.batch_refill_depth = int(batch_refill_depth)
        self._pacer = _Pacer(self.sustainable_rate_per_s)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue = []                 # list[_Pending] awaiting cohort assignment
        self._seq = 0
        self._stop = False
        self._planner = None
        self._rt_pool = ThreadPoolExecutor(max_workers=int(realtime_concurrency), thread_name_prefix="storm-rt")
        self._route_pool = ThreadPoolExecutor(max_workers=int(route_concurrency), thread_name_prefix="storm-route")
        self._batch_pool = ThreadPoolExecutor(max_workers=int(batch_concurrency), thread_name_prefix="storm-batch")
        self._inflight = []              # routing/batch futures, so close() can drain to completion (N:N on shutdown)
        self._rt_reserved = []           # monotonic ts of realtime slots RESERVED in the rolling horizon (I20: reserve
        #                                  realtime capacity ACROSS overlapping cohorts so a sustained fan overflows to
        #                                  batch instead of getting a fresh full budget every cohort)
        # observability — honest, per-instance evidence the tests assert on (never spendguard's own logs)
        self.cohorts = 0
        self.realtime_served = 0
        self.realtime_429_rerouted = 0
        self.batch_served = 0
        self.batch_jobs = 0
        self.close_abandoned = 0        # futures close() stopped waiting on at its drain deadline — SURFACED, never silent

    @property
    def realtime_budget(self):
        """How many requests the sustainable rate can clear within the urgency horizon — the COMBO split point."""
        return max(1, int(self.sustainable_rate_per_s * self.horizon_s))

    def _available_realtime(self):
        """How much of the realtime budget is FREE right now, after subtracting realtime RESERVED by overlapping
        cohorts in the last horizon (I20). A sustained fan therefore shares one budget-per-horizon across cohorts —
        once it is spent, further cohorts route entirely to batch — instead of each cohort claiming a fresh full
        budget (which would grow the realtime backlog unboundedly and never engage batch)."""
        now = time.monotonic()
        with self._lock:
            self._rt_reserved = [t for t in self._rt_reserved if now - t < self.horizon_s]
            return max(0, self.realtime_budget - len(self._rt_reserved))

    def _reserve_realtime(self, n):
        """SETUP/TEST helper only — unconditionally add `n` reservations. The HOT PATH must use _claim_realtime (which
        checks-and-reserves atomically); a check-then-reserve against this would race two concurrent cohorts."""
        if n <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._rt_reserved.extend([now] * int(n))

    def _claim_realtime(self, n_requested):
        """Atomically reserve up to `n_requested` realtime slots from the rolling-horizon budget; returns how many were
        GRANTED (0..n_requested). ONE lock hold for prune+check+reserve, so two concurrent cohorts can NOT both observe
        the same free capacity and each claim it (the I20 check-then-reserve TOCTOU) — aggregate realtime across
        overlapping cohorts stays ≤ budget per horizon, so a sustained fan's overflow diverts to batch."""
        now = time.monotonic()
        with self._lock:
            self._rt_reserved = [t for t in self._rt_reserved if now - t < self.horizon_s]
            avail = max(0, self.realtime_budget - len(self._rt_reserved))
            grant = min(max(0, int(n_requested)), avail)
            if grant:
                self._rt_reserved.extend([now] * grant)
            return grant

    # ── submission ────────────────────────────────────────────────────────────────────────────────────────────
    def submit_request(self, req):
        """Enqueue one request; returns a Future resolved EXACTLY ONCE with its result dict. Thread-safe; lazily starts
        the background planner. (Named submit_request, not submit, to stay unique vs lane_queue.submit — see
        docs/NAME_REGISTRY: a coalescer method and a queue-enqueue function are different jobs.)"""
        with self._cond:
            if self._stop:
                raise RuntimeError("StormCoalescer is closed")
            p = _Pending(req, self._seq)
            self._seq += 1
            self._queue.append(p)
            if self._planner is None:
                self._planner = threading.Thread(target=self._plan_loop, name="storm-planner", daemon=True)
                self._planner.start()
            self._cond.notify()
        return p.future

    def submit_all(self, reqs):
        """Submit an iterable of requests at once (the one-caller fan shape); returns futures in submission order."""
        return [self.submit_request(r) for r in reqs]

    # ── the planner: accumulate a cohort at a LOGICAL BREAK, then hand it to routing (never block here) ──────────
    def _plan_loop(self):
        while True:
            cohort = None
            try:
                cohort = self._drain_cohort()
                if cohort is None:
                    return
                fut = self._route_pool.submit(self._route_cohort, cohort)
                fut.add_done_callback(self._drop_inflight)
                with self._lock:
                    self._inflight.append(fut)
            except BaseException:
                # the planner must NEVER die silently and strand pending futures (e.g. route pool shut down in a close
                # race). Resolve whatever it just drained, then exit — close() has set _stop, or the pool is gone.
                if cohort:
                    self._resolve_error(cohort, "planner stopped")
                return

    def _drop_inflight(self, fut):
        """Remove a completed route/batch future from _inflight (a done-callback) so a long-running coalescer does not
        leak one Future per cohort for its lifetime."""
        with self._lock:
            try:
                self._inflight.remove(fut)
            except ValueError:
                pass

    def _drain_cohort(self):
        """Block for the first pending, then accumulate until the FIRST logical break: (a) quiescence — no new arrival
        within idle_gap; (b) max-wait ceiling; (c) early storm — already ≥ realtime_budget. Returns list[_Pending], or
        None to stop. The debounce (reset-on-arrival) is what cuts the cohort where it makes sense, never mid-fill."""
        with self._cond:
            while not self._queue and not self._stop:
                self._cond.wait()
            if self._stop and not self._queue:
                return None
            t0 = time.monotonic()
            while True:
                have = len(self._queue)
                if have >= self.realtime_budget:
                    break                                            # (c) already a storm — plan now, don't wait
                if time.monotonic() - t0 >= self.max_wait_s:
                    break                                            # (b) max-wait ceiling
                before = have
                self._cond.wait(timeout=self.idle_gap_s)
                if len(self._queue) == before and not self._stop:
                    break                                            # (a) quiescence — the burst has landed
                if self._stop:
                    break
            cohort, self._queue = self._queue, []
            return cohort

    # ── routing: the COMBO ──────────────────────────────────────────────────────────────────────────────────────
    def _route_cohort(self, cohort):
        with self._lock:
            self.cohorts += 1
        grant = self._claim_realtime(len(cohort))                  # I20: ATOMIC check-and-reserve (no cross-cohort TOCTOU)
        realtime = cohort[:grant]
        overflow = list(cohort[grant:])
        try:
            rt_reroute = self._run_realtime_paced(realtime)         # paced; returns items to re-route to batch (429 or error)
        except BaseException as e:                                  # e.g. realtime pool shut down during close — never strand
            self._resolve_error(realtime, "realtime: %s" % (str(e)[:120]))
            rt_reroute = []
        overflow.extend(rt_reroute)
        if overflow:
            try:
                fut = self._batch_pool.submit(self._run_batch_overflow, overflow, 0)   # batch may block minutes → own pool
                fut.add_done_callback(self._drop_inflight)
                with self._lock:
                    self._inflight.append(fut)
            except RuntimeError as e:                               # batch pool already shut down → resolve, never hang overflow
                self._resolve_error(overflow, "batch pool closed: %s" % (str(e)[:80]))

    def _run_realtime_paced(self, pend):
        """Release each realtime call through the SHARED pacer (≤ sustainable rate across all cohorts), execute on the
        realtime pool, resolve served ones; RETURN the _Pending items to RE-ROUTE to batch — a 429/529 OR an error
        result (so no 429 ever surfaces AND a failed realtime call is retried via batch rather than silently resolved
        as a success, I4). Each worker handles its OWN outcome and NEVER raises out, so one failing call can neither
        abort the others nor lose a sibling's reroute (the single-exception-aborts-the-loop defect)."""
        if not pend:
            return []
        reroute = []
        rl_lock = threading.Lock()

        def _serve_one_realtime(p):
            try:
                self._pacer.wait()
                r = self._execute_realtime(p.req)
            except BaseException as e:                              # a worker exception is THIS item's failure, not the cohort's
                r = {"text": None, "error": "realtime: %s" % (str(e)[:120]), "status_code": None}
            if _is_rate_limited(r) or (isinstance(r, dict) and r.get("error")):
                with rl_lock:
                    reroute.append(p)                              # 429 or error → batch (never surface, never silent success)
                return
            with self._lock:
                self.realtime_served += 1
            self._set(p, r, "realtime")

        futs = [self._rt_pool.submit(_serve_one_realtime, p) for p in pend]
        for f in futs:
            try:
                f.result()
            except BaseException:
                pass                                               # the worker already resolved/collected its own item
        if reroute:
            with self._lock:
                self.realtime_429_rerouted += len(reroute)
        return reroute

    def _run_batch_overflow(self, pend, depth):
        """Submit the overflow to the injected batch path, chunked by queue_planner.plan_batch_chunks, demuxing the
        output BY REQUEST ID — never positionally. Real Batch APIs return unordered and may drop/duplicate, so a
        positional zip corrupts demux (the Prompt 9 panel caught exactly this). Each _Pending's `seq` is its custom_id.
        A per-item failure (an error/rate-limit result, or a MISSING id) is ISOLATED (I19): only that id is refilled
        (bounded by batch_refill_depth), never its cohort-mates, and a succeeded sibling is never re-submitted (no
        double-spend, no I11/I15 corruption). Unknown ids in the output are quarantined (never assigned to anyone). A
        whole-submit EXCEPTION cannot be isolated per-item without bisection (follow-up I19-bisect) → that group
        resolves with a typed error (never a pending future, I12/I21)."""
        if not pend:
            return
        failed = []
        for group in self._slice_by_chunks(pend):
            if not group:
                continue
            by_id = {str(p.seq): p for p in group}           # seq is the stable custom_id for demux
            try:
                out = self._execute_batch([(str(p.seq), p.req) for p in group])
            except BaseException as e:                       # whole-submit failure: typed-error the group (never pending)
                self._resolve_error(list(by_id.values()), "batch submit: %s" % (str(e)[:120]))
                continue
            out = out if isinstance(out, dict) else {}
            with self._lock:
                self.batch_jobs += 1
            served = 0
            for cid, p in by_id.items():                     # demux BY ID — a missing/errored id is isolated, not zipped
                r = out.get(cid)
                if r is None or _is_rate_limited(r) or (isinstance(r, dict) and r.get("error")):
                    failed.append(p)
                else:
                    self._set(p, r, "batch")
                    served += 1
            with self._lock:
                self.batch_served += served
        if failed:
            if depth < self.batch_refill_depth:
                self._run_batch_overflow(failed, depth + 1)           # refill ONLY the failed ids (same custom_id → idempotent-ready)
            else:
                self._resolve_error(failed, "batch item unresolved after %d refill(s)" % self.batch_refill_depth)

    def _slice_by_chunks(self, pend):
        """Split `pend` into Batch-API-sized groups using the canonical planner (int COUNTS). Covers [0,len) exactly —
        the tail is always included even if the plan under-counts (I14: union == overflow, no drop)."""
        plan = queue_planner.plan_batch_chunks(len(pend), self.provider)
        counts = [c for c in (plan.get("chunks") or []) if isinstance(c, int) and c > 0]
        groups, i = [], 0
        for n in counts:
            if i >= len(pend):
                break
            groups.append(pend[i:i + n])
            i += n
        if i < len(pend):
            groups.append(pend[i:])                                  # never drop the tail
        return groups or [pend]

    # ── future resolution (exactly once) ─────────────────────────────────────────────────────────────────────────
    def _set(self, p, r, via):
        # set_result raises InvalidStateError if a RACING path already resolved this future — swallow it so resolution
        # is idempotent and exactly-once under concurrency (the check-then-set TOCTOU would crash the worker instead).
        try:
            p.future.set_result(dict(r, served_via=via) if isinstance(r, dict) else r)
        except InvalidStateError:
            pass

    def _resolve_error(self, pends, msg):
        for p in pends:
            try:
                p.future.set_result({"text": None, "error": "coalescer: %s" % msg, "provider": self.provider,
                                     "served_via": "error", "status_code": None})
            except InvalidStateError:
                pass

    def close(self, wait=True, drain_timeout_s=None):
        """Stop accepting work, let the planner exit, and (if wait) DRAIN in-flight routing/batch work so every future
        is resolved before shutdown (N:N on shutdown) — BOUNDED by drain_timeout_s so a HUNG batch can never hang
        close() itself (the sync→async guard applies to shutdown too). On timeout, stop waiting and shut the pools down
        without blocking; a stuck worker is a daemon thread that dies with the process. Idempotent."""
        with self._cond:
            if self._stop:
                return
            self._stop = True
            self._cond.notify_all()
        deadline = (time.monotonic() + float(drain_timeout_s if drain_timeout_s is not None
                                             else (self.max_wait_s * 3 + 10.0))) if wait else None
        if self._planner is not None:
            self._planner.join(timeout=(max(0.0, deadline - time.monotonic()) if deadline else None))
        timed_out = False
        if wait:
            # Do NOT clear _inflight — a route future appends its batch future LATER (after it completes), so clearing
            # the snapshot would lose that work and let a transient-empty list break the loop early (then a wait=True
            # pool shutdown would block on the hung batch anyway). Instead keep _inflight authoritative (the
            # done-callback prunes finished futures), bound everything by the WALL-CLOCK deadline, and grace-recheck
            # the empty case once for the append race.
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                with self._lock:
                    fs = [f for f in self._inflight if not f.done()]
                if not fs:
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))   # grace: a route future may still append a batch future
                    with self._lock:
                        fs = [f for f in self._inflight if not f.done()]
                    if not fs:
                        break                                      # truly idle — all route + batch work resolved
                    continue
                for f in fs:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        f.result(timeout=remaining)
                    except BaseException:
                        pass                                       # a hung/errored future must not hang close()
                if timed_out:
                    break
            if timed_out:
                with self._lock:
                    self.close_abandoned = sum(1 for g in self._inflight if not g.done())
        if timed_out:
            # SURFACE the abandonment — a silent timeout-cap would hide a hung batch (the coding:python finding). The
            # caller already holds every result (collected or typed-backpressure above); these futures resolve late or
            # not at all, and `close_abandoned` records how many for the caller/metrics to see.
            import sys as _sys
            print("[spendguard] storm_coalescer.close(): drain deadline reached — ABANDONED %d in-flight future(s) "
                  "(a hung/slow batch worker); shutting pools down without waiting on it." % self.close_abandoned,
                  file=_sys.stderr)
        _wait_pools = wait and not timed_out                       # on a hung worker, do NOT block shutdown on it
        self._route_pool.shutdown(wait=_wait_pools)
        self._batch_pool.shutdown(wait=_wait_pools)
        self._rt_pool.shutdown(wait=_wait_pools)
