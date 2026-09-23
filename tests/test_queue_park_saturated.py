"""Guard — PARKING (Step 4): a queued task that could not get a governor slot (reason='dispatch_saturated') NEVER RAN,
so it is DEFERRED and retried when capacity frees — real backpressure — instead of being failed or immediately
re-saturated. This is what makes 'no user sees a 429' hold under the DURABLE queue at high utilization.

  (1) a saturated result PARKS: state→pending, defer_until in the FUTURE, parks+1, and the lease's failure-attempt is
      REFUNDED (a capacity block is free — it never ran);
  (2) a parked (deferred) row is NOT leased until its defer window passes;
  (3) success → done; a GENUINE failure retries (pending, NO defer) and DOES count an attempt (unchanged);
  (4) the park cap (MAX_PARKS) → failed (a no-SLA task can't park forever);
  (5) a park that would push past the row's SLA deadline_ts → failed (honest deadline, never a silent breach).
Hermetic: the real durable queue on an isolated SPENDGUARD_HOME; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-park-")

from spendguard import lane_queue as lq

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

def _row(rid):
    c = lq._queue_conn()
    return c.execute("SELECT state, defer_until, attempts, parks, deadline_ts FROM lane_queue WHERE id=?",
                     (rid,)).fetchone()

SAT = {"text": None, "error": "queue full: no lane slot within 60s", "reason": "dispatch_saturated"}
FAIL = {"text": None, "error": "vendor 500", "reason": "api_error"}
OK = {"text": "answer", "lane": "codex"}

# ── (1) saturated → PARK ──
print("-- (1) a dispatch_saturated task is PARKED (deferred, park counted, attempt refunded) --")
[rid] = lq._enqueue_leased("park:one", ["task-a"])         # leased, attempts=1, parks=0
lq.settle(rid, SAT)
st = _row(rid)
ck("saturated → state 'pending' (parked, not failed)", st[0] == "pending")
ck("parked with a defer_until in the FUTURE", bool(st[1]) and st[1] > lq._iso(lq._utcnow()))
ck("park counted (parks 0→1)", st[3] == 1)
ck("the lease's failure-attempt is REFUNDED (attempts 1→0 — a capacity block is free)", st[2] == 0)

# ── (2) a parked row is NOT leased while deferred ──
print("-- (2) a parked (deferred) row is not leased until its defer window passes --")
got = lq.lease(10)                                         # rid is the only row and it is deferred ~10s out
ck("the deferred row is not handed out by lease()", all(r["id"] != rid for r in got))

# ── (3) success → done; genuine failure → pending+attempt, no defer ──
print("-- (3) success → done; a genuine (non-saturation) failure retries with NO defer and counts an attempt --")
[rid2] = lq._enqueue_leased("park:two", ["task-b"])
lq.settle(rid2, OK)
ck("success → done", _row(rid2)[0] == "done")
[rid3] = lq._enqueue_leased("park:three", ["task-c"])     # attempts=1, maxa=3
lq.settle(rid3, FAIL)
st3 = _row(rid3)
ck("genuine failure → pending (retry) with NO defer_until", st3[0] == "pending" and st3[1] is None)
ck("genuine failure counts the attempt (NOT refunded)", st3[2] == 1 and st3[3] == 0)

# ── (4) the park cap → failed ──
print("-- (4) MAX_PARKS reached → failed (a no-SLA task cannot park forever) --")
[rid4] = lq._enqueue_leased("park:four", ["task-d"])
c = lq._queue_conn(); c.execute("UPDATE lane_queue SET parks=? WHERE id=?", (lq.MAX_PARKS_DEFAULT, rid4)); c.commit()
lq.settle(rid4, SAT)
ck("a saturated task at the park cap → failed", _row(rid4)[0] == "failed")

# ── (5) a park that would breach the SLA deadline → failed ──
print("-- (5) a park that would push past the SLA deadline_ts → failed (honest, never a silent breach) --")
[rid5] = lq._enqueue_leased("park:five", ["task-e"])
c = lq._queue_conn(); c.execute("UPDATE lane_queue SET deadline_ts=? WHERE id=?", (lq._iso(lq._utcnow()), rid5)); c.commit()
lq.settle(rid5, SAT)                                       # defer = now+backoff >= deadline(now) → over the SLA
ck("a saturated task whose park would miss its SLA → failed", _row(rid5)[0] == "failed")

print(f"\n{'[FAIL]' if _fails else 'OK'} test_queue_park_saturated: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
