"""Durable lane work queue — the PENDING side + leasing drainer that turns "high utilization → fail/bill" into
"high utilization → queue at $0, drain on idle capacity". Guards the behaviours the design promises: enqueue never
runs; lease is priority-ordered, intent-UNIFORM, and atomic (attempts+1, leased); settle records done / retries /
fails-when-exhausted; an EXPIRED lease is RECLAIMED so a crashed worker never loses work; drain runs the batch
through bulk_delegate and empties the queue; and the local-load ceiling pauses leasing. Offline: bulk_delegate +
loadavg stubbed, isolated db, no LLM, no subprocess.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-queue-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue as q                                                 # noqa: E402
from spendguard import lane_balance                                                    # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- enqueue accepts work without running it; depth reflects pending --")
i1 = q.enqueue("intentA", "task-a1", priority=1)
i2 = q.enqueue("intentA", "task-a2", priority=1)
i3 = q.enqueue("intentB", "task-b1", priority=5)   # higher priority, different intent
fails += ck("enqueue returns row ids", all(isinstance(x, int) for x in (i1, i2, i3)))
d = q.queue_depth()
fails += ck("all 3 are pending, none run", d.get("pending") == 3 and d.get("done", 0) == 0)

print("\n-- lease: highest-priority intent first, INTENT-UNIFORM, marks leased --")
batch = q.lease(10)
fails += ck("top-priority intent (B) leased alone (intent-uniform, not mixed with A)",
            len(batch) == 1 and batch[0]["intent"] == "intentB")
d = q.queue_depth()
fails += ck("one leased, two still pending", d.get("leased") == 1 and d.get("pending") == 2)

print("\n-- settle: success → done --")
q.settle(batch[0]["id"], {"text": "ok-b1", "lane": "gemini", "billed": False})
fails += ck("settled task is done", q.queue_depth().get("done") == 1)

print("\n-- settle: a failure RETRIES while attempts remain, then FAILS when exhausted --")
rid = q.enqueue("retryI", "flaky", priority=9, max_attempts=2)     # high priority so it's the one leased next
L1 = q.lease(1)                                                    # attempt 1
fails += ck("the flaky task was leased", L1 and L1[0]["id"] == rid)
q.settle(L1[0]["id"], {"error": "lane down"})
L2 = q.lease(1)                                                    # attempt 2 (exhausts max_attempts=2)
fails += ck("after a failing attempt (1<2) the SAME task is re-leased (retry)", L2 and L2[0]["id"] == rid)
q.settle(L2[0]["id"], {"error": "lane down again"})
fails += ck("exhausted task is failed (not retried forever)", q.queue_depth().get("failed") == 1)

print("\n-- RECLAIM: an expired lease (crashed worker) returns to pending on the next lease --")
q.enqueue("crashI", "orphan", priority=9)                          # high priority so it's the focus of the next lease
crashed = q.lease(1, lease_s=-10)                                  # lease with an already-expired window (worker 'died')
fails += ck("orphan is leased (worker took it)", crashed and crashed[0]["task"] == "orphan")
reclaimed = q.lease(1)                                             # next lease reclaims the expired one and re-serves it
fails += ck("expired lease reclaimed → same task re-leased (work not lost)",
            reclaimed and reclaimed[0]["task"] == "orphan")
q.settle(reclaimed[0]["id"], {"text": "recovered", "lane": "codex"})
fails += ck("reclaimed task then completes (nothing stuck leased from it)", q.queue_depth().get("leased") == 0)

print("\n-- drain: leases → bulk_delegate → settle, empties the queue; spreads across lanes; a bad task fails --")
_orig_bulk = lane_balance.bulk_delegate
lane_balance.bulk_delegate = lambda tasks, intent, system=None, reasoning=None, **kw: [
    {"text": None, "lane": None, "billed": False, "error": "boom"} if t == "BOOM"
    else {"text": f"ans::{t}", "lane": ["gemini", "codex"][k % 2], "use_name": "x", "billed": False, "error": None}
    for k, t in enumerate(tasks)]
try:
    for n in range(6):
        q.enqueue("drainI", f"d{n}", priority=2)
    q.enqueue("drainI", "BOOM", priority=2, max_attempts=1)        # one task that always errors (max 1 → fails fast)
    s = q.drain(batch=3, idle_rounds=1, idle_sleep=0.0)
    fails += ck("drain reports work ran", s["ran"] >= 7 and s["rounds"] >= 1)
    fails += ck("drain spread the good tasks across both lanes", set(s["by_lane"]) == {"gemini", "codex"})
    dd = q.queue_depth()
    fails += ck("queue fully drained (nothing pending or leased) and the BOOM task is failed",
                dd.get("pending", 0) == 0 and dd.get("leased", 0) == 0 and dd.get("failed", 0) >= 1)
finally:
    lane_balance.bulk_delegate = _orig_bulk

print("\n-- CRASH-RESUME: tasks leased by a dead worker are picked up by a fresh drain --")
lane_balance.bulk_delegate = lambda tasks, intent, system=None, reasoning=None, **kw: [
    {"text": f"r::{t}", "lane": "gemini", "billed": False, "error": None} for t in tasks]
try:
    q.enqueue("resumeI", "surv-1")
    q.enqueue("resumeI", "surv-2")
    q.lease(10, lease_s=-5)                                        # a worker leases both, then 'crashes' (never settles)
    fails += ck("both tasks are stuck leased after the crash", q.queue_depth().get("leased") == 2)
    s2 = q.drain(batch=10, idle_rounds=1, idle_sleep=0.0)          # a fresh drain reclaims + finishes them
    fails += ck("fresh drain recovered the crashed work (both done, none stuck)",
                s2["done"] == 2 and q.queue_depth().get("leased") == 0)
finally:
    lane_balance.bulk_delegate = _orig_bulk

print("\n-- local-load ceiling: _overloaded honours the threshold; drain pauses when over it --")
fails += ck("ceiling off (0) → never overloaded", q._overloaded(0) is False)
_orig_load = os.getloadavg
try:
    os.getloadavg = lambda: (9.9, 9.9, 9.9)
    fails += ck("load 9.9 over ceiling 1.0 → overloaded", q._overloaded(1.0) is True)
    fails += ck("load 9.9 under ceiling 100 → not overloaded", q._overloaded(100.0) is False)
    q.enqueue("loadI", "should-not-run-while-overloaded")
    calls = {"n": 0}
    lane_balance.bulk_delegate = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), [])[1]
    s3 = q.drain(batch=5, load_ceiling=1.0, idle_sleep=0.0, max_iters=1)    # overloaded → should NOT lease/run
    fails += ck("drain ran no work while the machine was over the load ceiling",
                calls["n"] == 0 and q.queue_depth().get("pending") == 1)
finally:
    os.getloadavg = _orig_load
    lane_balance.bulk_delegate = _orig_bulk

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_queue: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
