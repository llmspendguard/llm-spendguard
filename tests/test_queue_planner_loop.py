"""Item 3 (c) — the PERIODIC planner cadence: queue_planner.plan_loop runs tick() on a fixed ~1s interval (Ash:
"a periodic say every second planning"), the continuous look-ahead the drain's per-round consult cannot give.

Guards: it runs exactly the requested number of ticks and calls the observer each time; it is $0 / NO execution (never
consumes or mutates queued rows — the drain stays the single executor); it stops PROMPTLY on a stop_event; and a
DELIBERATE stop from tick OR from the observer PROPAGATES, never swallowed into 'keep polling' (refusal-containment).

Offline + deterministic ($0). Isolation: SPENDGUARD_HOME → tempfile.mkdtemp before importing spendguard.
"""
import os, sys, tempfile, threading

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-planloop-")

from spendguard import queue_planner as qp, lane_queue as lq, gate   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = Checks()

    # 1. runs exactly `iterations` ticks, observer called each time, observable termination
    seen = []
    res = qp.plan_loop(iterations=3, interval_s=0.05, on_tick=lambda t: seen.append(t))
    c.ck("plan_loop ran exactly 3 ticks", res.get("ticks") == 3, str(res))
    c.ck("observer called once per tick", len(seen) == 3, str(len(seen)))
    c.ck("termination is observable (stopped_by='iterations')", res.get("stopped_by") == "iterations", str(res))
    c.ck("each tick carries a forecast + offload plan", all("forecast" in t and "offload" in t for t in seen))

    # 2. $0 / NO EXECUTION — a queued row is not consumed or mutated by planning
    lq.enqueue("planner-noexec", "task")
    before = lq.queue_depth()
    qp.plan_loop(iterations=2, interval_s=0.05, on_tick=lambda t: None)
    after = lq.queue_depth()
    c.ck("planning does not mutate the queue (pending unchanged)",
         before.get("pending") == after.get("pending"), "before=%s after=%s" % (before, after))

    # 3. stop_event stops it PROMPTLY (pre-set event → stops right after the first tick)
    ev = threading.Event()
    ev.set()
    res2 = qp.plan_loop(interval_s=30.0, on_tick=lambda t: None, stop_event=ev)   # 30s interval, but pre-set → no wait
    c.ck("stop_event halts promptly (1 tick, stopped_by='stop_event')",
         res2.get("ticks") == 1 and res2.get("stopped_by") == "stop_event", str(res2))

    # 4. a DELIBERATE stop from the observer PROPAGATES (never swallowed → 'keep polling')
    def _refuse(_t):
        raise gate.SpendGateRefused("budget cap hit during observe")
    propagated = False
    try:
        qp.plan_loop(iterations=5, interval_s=0.05, on_tick=_refuse)
    except Exception as e:
        propagated = gate.is_deliberate_stop(e)
    c.ck("deliberate stop from on_tick propagates (refusal-containment)", propagated)

    # 5. a NON-deliberate observer hiccup does NOT kill the loop (it keeps planning) — AND is SURFACED, never silently
    #    swallowed (F1): a loop must not report a clean run when its sole observer failed every tick.
    def _flaky(_t):
        raise ValueError("observer bug, not a refusal")
    res3 = qp.plan_loop(iterations=2, interval_s=0.05, on_tick=_flaky)
    c.ck("non-deliberate observer error does not stop the loop", res3.get("ticks") == 2, str(res3))
    c.ck("observer errors are COUNTED + surfaced in the return (not silently swallowed)",
         res3.get("tick_observer_errors") == 2, str(res3))
    res4 = qp.plan_loop(iterations=2, interval_s=0.05, on_tick=lambda _t: None)   # a WORKING observer → zero errors
    c.ck("a healthy observer reports 0 tick_observer_errors", res4.get("tick_observer_errors") == 0, str(res4))

    # 6. a FAILED tick (tick() itself raises non-deliberate) is COUNTED + surfaced (tick_failures), never reported as a
    #    clean completed tick — a run where every tick raised must be distinguishable from a successful run (F1).
    _real_tick = qp.tick
    qp.tick = lambda **k: (_ for _ in ()).throw(ValueError("tick blew up"))
    try:
        res5 = qp.plan_loop(iterations=2, interval_s=0.05, on_tick=None)
    finally:
        qp.tick = _real_tick
    c.ck("a failed tick is counted in tick_failures (not indistinguishable from a clean tick)",
         res5.get("tick_failures") == 2, str(res5))

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_planner_loop: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
