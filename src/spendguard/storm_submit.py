"""submit_storm — the governed SYNCHRONOUS-storm entry: hand it N tasks + a model + an urgency horizon and it returns
N results, pacing the share realtime can sustain under the (xp_rate-governed) wall and diverting the overflow to the
Batch API, so a burst of synchronous callers never blocks thousands of threads for minutes (the panel's sync→async
deadlock) and never surfaces a 429. It is the COMBO (pace + batch) the 429-storm plan requires, built on the proven
StormCoalescer engine (tests/test_storm_coalescer.py) + the cross-process rate limiter already wired into dispatch.

CONTRACT (differs from bulk_delegate's on_miss='batch', which is ASYNC and returns queued_batch HANDLES): submit_storm
COLLECTS — every submitted task gets a final result back, demuxed by id, realtime or batch, in submission order.

INJECTION: `execute_batch` is REQUIRED and provider-aware — items=[(custom_id, prompt)] -> {custom_id: result}. A
production caller passes an executor that submits to the provider's Batch API and collects (openai→submit_chat_tasks +
collect_chat_tasks; anthropic→Message Batches); the acceptance test passes a FakeProvider-backed one. We do NOT ship a
default here because a correct provider-aware submit+collect (id-keyed, deadline-aware so a multi-hour batch ETA on a
tight deadline becomes typed backpressure, not a blocked thread) is its own scoped build — shipping an unexercised
default would be the 'capability built but not wired' shortcut. realtime, by contrast, rides the real governed metered
path here (adapters.call → dispatch.admit → the cross-process rate window → _call_guarded)."""
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


def submit_storm(tasks, intent, model, execute_batch, deadline_s=30.0, system=None, reasoning="minimal",
                 prompt_for=None, max_workers=16, idle_gap_s=0.01, max_wait_s=1.0):
    """Submit N tasks through the pace+batch combo and COLLECT N results (submission order). `execute_batch` is the
    REQUIRED provider-aware batch executor (see module docstring). `prompt_for(task)` maps a task to its prompt string
    (default: the task IS the prompt). `deadline_s` is the async urgency horizon that sizes the realtime budget
    (rate × horizon); the overflow diverts to batch. Returns a list of result dicts aligned to `tasks`."""
    if not tasks:
        return []
    provider = adapters.provider_for(model)
    rate = sustainable_rate_per_s(provider, model)
    coalescer = StormCoalescer(
        execute_realtime=_realtime_executor(model, system, reasoning, intent, deadline_s),
        execute_batch=execute_batch, sustainable_rate_per_s=rate, horizon_s=deadline_s,
        realtime_concurrency=max_workers, idle_gap_s=idle_gap_s, max_wait_s=max_wait_s, provider=provider)
    _pf = prompt_for if callable(prompt_for) else (lambda t: t)
    try:
        futures = coalescer.submit_all([_pf(t) for t in tasks])
        return [f.result() for f in futures]
    finally:
        coalescer.close()
