"""submit_storm — the governed SYNCHRONOUS-storm entry: hand it N tasks + a model + an urgency horizon and it returns
N results, pacing the share realtime can sustain under the (xp_rate-governed) wall and diverting the overflow to the
Batch API, so a burst of synchronous callers never blocks thousands of threads for minutes (the panel's sync→async
deadlock) and never surfaces a 429. It is the COMBO (pace + batch) the 429-storm plan requires, built on the proven
StormCoalescer engine (tests/test_storm_coalescer.py) + the cross-process rate limiter already wired into dispatch.

CONTRACT (differs from bulk_delegate's on_miss='batch', which is ASYNC and returns queued_batch HANDLES): submit_storm
COLLECTS — every submitted task gets a final result back, demuxed by id, realtime or batch, in submission order.

INJECTION: `execute_batch` is provider-aware — items=[(custom_id, prompt)] -> {custom_id: result}. It DEFAULTS to
`default_batch_executor(model, intent)` (below): the production seam that submits to the vendor's Batch API and
collects, id-keyed by custom_id (openai→submit_chat_tasks+collect_chat_tasks; anthropic→submit_message_batch+
collect_message_batch). A caller passes its own only to override (e.g. the acceptance test passes a FakeProvider-backed
one). The caller's blocked-thread risk is bounded by submit_storm's `collect_timeout_s` (a multi-hour batch ETA becomes
typed backpressure, not a hang), independent of how long the batch worker itself polls. realtime rides the real
governed metered path here (adapters.call → dispatch.admit → the cross-process rate window → _call_guarded)."""
import time
from concurrent.futures import TimeoutError as FuturesTimeout

from . import adapters, dispatch
from .storm_coalescer import StormCoalescer


def sustainable_rate_per_s(provider, model):
    """The vendor's sustainable realtime requests/second = published rpm / 60 (from the catalog cold-cap / config /
    learned limits, via dispatch.effective_limits). Raises if unknown — the combo cannot size a realtime budget
    without it, and guessing is exactly the kind of invented number this project forbids (seed the catalog cold cap)."""
    el = dispatch.effective_limits(provider, model) or {}
    rpm = int(el.get("rpm") or 0)
    if rpm <= 0:
        raise ValueError("no rpm known for %s:%s (source=%s) — seed the catalog cold cap before storm-submitting"
                         % (provider, model, el.get("source")))
    return rpm / 60.0


def _realtime_executor(model, system, reasoning, intent, deadline_s):
    """A governed metered realtime call. Rides the REAL admission path (dispatch.admit → the cross-process rate window
    → _call_guarded), metered (skip the $0 lane), never substituted, and `_route=False` so it does NOT re-enter the
    queue-record path (the same flag bulk_delegate sets for already-governed work) — no recursion."""
    def _run_governed_realtime(prompt):
        return adapters.call(model, prompt, system=system, reasoning=reasoning, sig=intent,
                             metered_only=True, governed=True, no_substitution=True,
                             _route=False, timeout_s=deadline_s)
    return _run_governed_realtime


def default_batch_executor(model, intent, system=None, reasoning="minimal", poll_interval_s=5.0, max_poll_s=86400.0):
    """The PRODUCTION provider-aware `execute_batch` submit_storm defaults to: submit the cohort to the vendor's Batch
    API keyed by custom_id, POLL until the batch ends, collect, and return {custom_id: result dict}. openai →
    submit.submit_chat_tasks + callio.collect_chat_tasks; anthropic → submit.submit_message_batch +
    callio.collect_message_batch (both ~half the realtime price, id-keyed by custom_id — the demux the coalescer needs).
    An UNSUPPORTED provider raises (fail-loud — route realtime/lane instead, never a silent wrong path). It blocks the
    BATCH-worker thread until the batch ends (bounded by max_poll_s); submit_storm's collect_timeout_s bounds the CALLER
    independently, so a slow batch never blocks the caller. A whole-submit failure returns a per-item error (never a
    silent drop); a per-request failure is surfaced by id."""
    from . import adapters as _adapters, callio as _callio, submit as _submit   # lazy — avoid import-time cycles
    provider = _adapters.provider_for(model)
    _pairs = {"openai": (_submit.submit_chat_tasks, _callio.collect_chat_tasks),
              "anthropic": (_submit.submit_message_batch, _callio.collect_message_batch)}
    pair = _pairs.get(provider)
    if pair is None:
        raise ValueError("no Batch API wired for provider %r (model %r) — supported: %s; route realtime/lane instead"
                         % (provider, model, sorted(_pairs)))
    submit_fn, collect_fn = pair

    def _execute_batch(items):
        tasks = [{"custom_id": str(cid), "content": prompt} for cid, prompt in items]
        kw = {"intent": intent}
        if provider == "openai":
            kw["reasoning"] = reasoning                       # the chat Batch API takes reasoning; the Messages one does not
        if system is not None:
            kw["system"] = system
        sub = submit_fn(tasks, model, **kw) or {}
        bid = sub.get("batch_id")
        if not bid:                                           # whole-submit failure → per-item error, never a silent drop
            err = sub.get("error") or "batch submit returned no batch_id"
            return {str(cid): {"text": None, "status_code": None, "error": "batch submit: %s" % err}
                    for cid, _ in items}
        served, failed, t0, timed_out = {}, {}, time.monotonic(), False
        while True:                                           # poll until the batch ENDS (bounded by max_poll_s)
            col = collect_fn(bid, intent, model, require_ready=True) or {}
            served.update(col.get("results") or {})
            failed.update(col.get("failed") or {})
            if not col.get("not_ready"):
                break
            if (time.monotonic() - t0) >= max_poll_s:
                timed_out = True
                break
            time.sleep(poll_interval_s)
        res = {str(cid): {"text": txt, "status_code": 200, "provider": provider} for cid, txt in served.items()}
        for cid, err in failed.items():
            res[str(cid)] = {"text": None, "status_code": None, "error": "batch item: %s" % err}
        # SURFACE any id neither served nor failed (poll ceiling hit, or the collector dropped it) — NEVER a silent drop
        pending = [str(cid) for cid, _ in items if str(cid) not in res]
        if pending:
            import sys as _sys
            print("[spendguard] default_batch_executor: batch %s left %d request(s) UNRESOLVED (%s; max_poll_s=%.0fs)"
                  " — returning typed backpressure, not a silent drop." % (bid, len(pending),
                  "poll ceiling reached" if timed_out else "collector omitted them", max_poll_s), file=_sys.stderr)
            for cid in pending:
                res[cid] = {"text": None, "status_code": None, "served_via": "batch_pending",
                            "error": "batch poll ceiling (max_poll_s=%.0fs) reached — request still pending" % max_poll_s}
        return res
    return _execute_batch


def submit_storm(tasks, intent, model, execute_batch=None, deadline_s=30.0, system=None, reasoning="minimal",
                 prompt_for=None, max_workers=16, idle_gap_s=0.01, max_wait_s=1.0, collect_timeout_s=1800.0):
    """Submit N tasks through the pace+batch combo and COLLECT N results (submission order). `execute_batch` defaults
    to `default_batch_executor(model, intent)` — the production provider-aware Batch-API seam (openai/anthropic); pass
    your own only to override it (e.g. a test). `prompt_for(task)` maps a task to its prompt string
    (default: the task IS the prompt). `deadline_s` is the async urgency horizon that sizes the realtime budget
    (rate × horizon); the overflow diverts to batch. `collect_timeout_s` BOUNDS the total wait: a request still
    unfinished by then returns TYPED BACKPRESSURE (served_via='backpressure') rather than blocking the caller
    indefinitely — so a hung or multi-hour batch can never deadlock the calling thread (the sync→async guard).
    Realtime + a fast batch resolve well within it. Returns a list of result dicts aligned to `tasks`."""
    tasks = list(tasks)                                   # materialize (a generator is truthy + single-use; see below)
    if not tasks:
        return []
    provider = adapters.provider_for(model)
    if not provider:                                      # fail LOUD on an unknown model, not an obscure downstream error
        raise ValueError("unknown model %r — adapters.provider_for returned no provider" % (model,))
    if execute_batch is None:                             # default to the production provider-aware Batch-API seam (4a)
        execute_batch = default_batch_executor(model, intent, system=system, reasoning=reasoning)
    rate = sustainable_rate_per_s(provider, model)
    coalescer = StormCoalescer(
        execute_realtime=_realtime_executor(model, system, reasoning, intent, deadline_s),
        execute_batch=execute_batch, sustainable_rate_per_s=rate, horizon_s=deadline_s,
        realtime_concurrency=max_workers, idle_gap_s=idle_gap_s, max_wait_s=max_wait_s, provider=provider)
    _pf = prompt_for if callable(prompt_for) else (lambda t: t)
    try:
        futures = coalescer.submit_all([_pf(t) for t in tasks])
        out, t0 = [], time.monotonic()
        for f in futures:
            remaining = collect_timeout_s - (time.monotonic() - t0)
            try:
                out.append(f.result(timeout=max(0.0, remaining)))
            except FuturesTimeout:                        # NEVER block indefinitely — typed backpressure, not a hung thread
                out.append({"text": None, "status_code": None, "served_via": "backpressure",
                            "error": "storm: result not ready within collect_timeout_s=%.0fs (batch ETA exceeded the "
                                     "wait) — typed backpressure, not a blocked thread" % collect_timeout_s})
        return out
    finally:
        # every result is already collected or typed-backpressured above, so do NOT re-block draining a stuck batch
        # future here (that would reintroduce the hang collect_timeout_s just prevented).
        coalescer.close(wait=False)
