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
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

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
        if sustainable_rate_per_s <= 0:
            raise ValueError("sustainable_rate_per_s must be > 0 (rpm/60 for the vendor); got %r" % (sustainable_rate_per_s,))
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
        # observability — honest, per-instance evidence the tests assert on (never spendguard's own logs)
        self.cohorts = 0
        self.realtime_served = 0
        self.realtime_429_rerouted = 0
        self.batch_served = 0
        self.batch_jobs = 0

    @property
    def realtime_budget(self):
        """How many requests the sustainable rate can clear within the urgency horizon — the COMBO split point."""
        return max(1, int(self.sustainable_rate_per_s * self.horizon_s))

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
            cohort = self._drain_cohort()
            if cohort is None:
                return
            fut = self._route_pool.submit(self._route_cohort, cohort)
            with self._lock:
                self._inflight.append(fut)

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
        budget = self.realtime_budget
        realtime = cohort[:budget]
        overflow = list(cohort[budget:])
        try:
            rt_429 = self._run_realtime_paced(realtime)              # paced; returns the items that still 429'd
        except BaseException as e:                                   # never leave a realtime future pending (I12)
            self._resolve_error(realtime, "realtime: %s" % (str(e)[:120]))
            rt_429 = []
        overflow.extend(rt_429)
        if overflow:
            fut = self._batch_pool.submit(self._run_batch_overflow, overflow, 0)   # batch may block minutes → own pool
            with self._lock:
                self._inflight.append(fut)

    def _run_realtime_paced(self, pend):
        """Release each realtime call through the SHARED pacer (≤ sustainable rate across all cohorts), execute on the
        realtime pool, resolve served ones; RETURN the _Pending items that came back rate-limited (→ re-routed to
        batch so no 429 ever surfaces, I4)."""
        if not pend:
            return []
        rate_limited = []
        rl_lock = threading.Lock()

        def _serve_one_realtime(p):
            self._pacer.wait()
            r = self._execute_realtime(p.req)
            if _is_rate_limited(r):
                with rl_lock:
                    rate_limited.append(p)
                return
            with self._lock:
                self.realtime_served += 1
            self._set(p, r, "realtime")

        futs = [self._rt_pool.submit(_serve_one_realtime, p) for p in pend]
        for f in futs:
            f.result()                                               # propagate a worker crash to _route_cohort's guard
        if rate_limited:
            with self._lock:
                self.realtime_429_rerouted += len(rate_limited)
        return rate_limited

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
        if p.future.done():
            return
        if isinstance(r, dict):
            p.future.set_result(dict(r, served_via=via))
        else:
            p.future.set_result(r)

    def _resolve_error(self, pends, msg):
        for p in pends:
            if not p.future.done():
                p.future.set_result({"text": None, "error": "coalescer: %s" % msg, "provider": self.provider,
                                     "served_via": "error", "status_code": None})

    def close(self, wait=True):
        """Stop accepting work, let the planner exit, and (if wait) DRAIN all in-flight routing/batch work so every
        future is resolved before shutdown (N:N on shutdown). Idempotent."""
        with self._cond:
            if self._stop:
                return
            self._stop = True
            self._cond.notify_all()
        if self._planner is not None:
            self._planner.join(timeout=self.max_wait_s * 3 + 5.0)
        if wait:
            while True:
                with self._lock:
                    fs = list(self._inflight)
                    self._inflight = []
                if not fs:
                    break
                for f in fs:
                    try:
                        f.result()
                    except BaseException:
                        pass
        self._route_pool.shutdown(wait=wait)
        self._batch_pool.shutdown(wait=wait)
        self._rt_pool.shutdown(wait=wait)
