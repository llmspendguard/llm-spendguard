"""GUARD — the SLA/priority SCHEDULER (Layer 2b): lease() serves the MOST URGENT pending work first.

'What is needed in the time that is available': the drain leases by highest priority, then REALTIME before batch,
then the tightest SLA deadline (deadline_ts asc, nulls last), then oldest. So a realtime item with a near deadline
preempts a batch backfill within the same capacity. This pins the total order by leasing one row at a time and
checking the sequence — and it enqueues the LEAST-urgent item FIRST (lowest id) so a correct result can NOT be mere
insertion order. Hermetic: the REAL durable queue in an isolated home; lease() only orders (runs no task); no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-sched-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


HI = lane_queue.PRIORITY_INTERACTIVE
LO = lane_queue.PRIORITY_BULK

# Enqueue LEAST-urgent FIRST (ids 1..4 in this order), so the correct lease order can't be insertion (id) order.
lane_queue.enqueue("batch-low", "t", priority=LO, sla_class="batch")                                          # id1 least urgent
lane_queue.enqueue("batch-high", "t", priority=HI, sla_class="batch")                                         # id2
lane_queue.enqueue("rt-loose", "t", priority=HI, sla_class="realtime", deadline_ts=lane_queue._deadline_iso(1000))   # id3
lane_queue.enqueue("rt-tight", "t", priority=HI, sla_class="realtime", deadline_ts=lane_queue._deadline_iso(10))     # id4 MOST urgent

order = []
for _ in range(4):
    got = lane_queue.lease(1)               # each lease claims the next most-urgent pending intent (leaves it 'leased')
    if got:
        order.append(got[0]["intent"])

print("-- leased order:", order)
ck("scheduler serves MOST-URGENT first: tight-SLA realtime > loose realtime > high-priority batch > low-priority batch",
   order == ["rt-tight", "rt-loose", "batch-high", "batch-low"])
ck("the urgent item (enqueued LAST, id=4) is leased FIRST — proves URGENCY order, not insertion order",
   bool(order) and order[0] == "rt-tight")
ck("a realtime item outranks a same-priority batch item",
   order.index("rt-loose") < order.index("batch-high"))
ck("the tighter SLA deadline outranks the looser one (same priority + class)",
   order.index("rt-tight") < order.index("rt-loose"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_queue_scheduler: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
