"""Pure escalation admission plus fail-safe zero-billable stall cancellation."""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import config


_ZERO_SAFE = {"subscription"}
_SIGNALLED = {"metered_streaming"}


def relative_escalation_gate(baseline, escalation, absolute_cap, *, quality_bar_met=True,
                             ratio_limit=None, baseline_epsilon=None):
    """Return the pure estimate-time decision for composed relative and absolute gates.

    ``baseline`` and ``escalation`` are estimates already produced by spendguard's pricing/expect
    machinery; this function deliberately does no token pricing of its own.
    """
    base = float(baseline)
    esc = float(escalation)
    cap = None if absolute_cap is None else float(absolute_cap)
    limit = config.escalation_ratio() if ratio_limit is None else float(ratio_limit)
    epsilon = config.escalation_baseline_epsilon() if baseline_epsilon is None else float(baseline_epsilon)
    deferred = base <= epsilon
    ratio = None if deferred else esc / base
    tripped = []
    if cap is not None and esc > cap:
        tripped.append("absolute")
    if not deferred and ratio > limit:
        tripped.append("relative")
    if not quality_bar_met:
        return {"proceed": False, "reason": "quality_bar_unmeet within budget", "baseline": base,
                "escalation": esc, "ratio": ratio, "needs_auth": bool(tripped),
                "tripped": tripped + ["quality"]}
    if tripped:
        return {"proceed": False, "reason": "+".join(tripped) + " gate tripped", "baseline": base,
                "escalation": esc, "ratio": ratio, "needs_auth": True, "tripped": tripped}
    reason = "relative gate deferred to absolute cap" if deferred else "relative and absolute gates passed"
    return {"proceed": True, "reason": reason, "baseline": base, "escalation": esc,
            "ratio": ratio, "needs_auth": False, "tripped": []}


def cancel_on_zero_billable(run: Callable[[], object], soft_deadline_s: float, *, transport: str,
                            cancel: Callable[[], bool] | None = None,
                            zero_billable_proven: Callable[[], bool] | None = None,
                            provider="unknown", model="unknown", intent=None):
    """Run a transport operation and cancel at its soft deadline only with proof of zero billable tokens.

    Supported proof contracts: ``subscription`` is always $0; ``metered_streaming`` additionally requires
    a provider-event observer in ``zero_billable_proven``. Non-streaming metered and CLI transports can
    never prove zero here and therefore run to completion. ``cancel`` must confirm that it stopped the request.
    """
    if soft_deadline_s <= 0:
        raise ValueError("soft_deadline_s must be > 0")
    box = {}
    done = threading.Event()

    def target():
        try:
            box["result"] = run()
        except BaseException as exc:  # preserve the transport's original behavior in the caller
            box["error"] = exc
        finally:
            done.set()

    started = time.monotonic()
    worker = threading.Thread(target=target, name="spendguard-zero-billable", daemon=True)
    worker.start()
    expired = not done.wait(float(soft_deadline_s))
    proof = transport in _ZERO_SAFE
    if transport in _SIGNALLED:
        proof = bool(zero_billable_proven and zero_billable_proven())
    cancelled = False
    if expired and proof and cancel is not None:
        cancelled = bool(cancel())
    if cancelled:
        from . import calls
        calls.record_call(provider, model, "subscription" if transport == "subscription" else "realtime", 0.0,
                          latency=time.monotonic() - started, intent=intent, outcome="deadline_exceeded",
                          disposition="cancelled-stall-zerobill",
                          disposition_reason="soft deadline exceeded; transport proved zero billable tokens")
        return {"result": None, "stall_outcome": "stalled_zerobill_cancelled", "cost": 0.0,
                "disposition": "cancelled-stall-zerobill"}
    done.wait()  # fail-safe: an unproved or unconfirmed cancellation never abandons a possibly billed request
    if "error" in box:
        raise box["error"]
    return {"result": box.get("result"), "stall_outcome": "produced_tokens_ran",
            "cost": None, "disposition": None}
