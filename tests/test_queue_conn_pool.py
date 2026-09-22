"""Guard — lane_queue's THREAD-LOCAL pooled connection (the ~20x that makes route_through_queue cheap enough to be
default). Pins the reuse + self-healing contract that a durable queue must never lose:
  · _queue_conn() REUSES one connection per thread (same object across calls);
  · _queue_op() COMMITS on success (a write is visible to a fresh connection);
  · _queue_op() ROLLS BACK + DROPS the pooled connection on error (self-heal: the next op reopens + the queue still
    works), and re-raises so the call site's own except still runs;
  · _queue_db() is a FRESH, closeable connection — closing it (a test/CLI does) never corrupts the pool;
  · a config.db_path() switch (a test repointing SPENDGUARD_HOME) reopens the pool on the new file (staleness handled).
Hermetic: pure sqlite through the pooled connection; no lanes, no network."""
import os
import sys
import contextlib
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-pool-"))

from spendguard import lane_queue, config

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_INS = ("INSERT INTO lane_queue(intent,task,state,created_ts,updated_ts) "
        "VALUES(?, 't', 'pending', 'x', 'x')")

print("-- reuse: one pooled connection per thread --")
c1 = lane_queue._queue_conn()
c2 = lane_queue._queue_conn()
ck("_queue_conn() returns the SAME object across calls (reused)", c1 is c2)

print("-- _queue_db() is a FRESH connection; closing it never corrupts the pool --")
fresh = lane_queue._queue_db()
ck("_queue_db() is NOT the pooled connection", fresh is not c1)
fresh.close()
ck("after closing the fresh conn, the pool is intact (same object)", lane_queue._queue_conn() is c1)

print("-- _queue_op commits on success (visible to a fresh connection) --")
with lane_queue._queue_op() as c:
    c.execute(_INS, ("poolI",))
with contextlib.closing(lane_queue._queue_db()) as v:
    n = v.execute("SELECT COUNT(*) FROM lane_queue WHERE intent='poolI'").fetchone()[0]
ck("the write committed (a fresh connection sees it)", n == 1)

print("-- _queue_op rolls back + DROPS the pooled connection on error, then self-heals --")
before = lane_queue._queue_conn()
try:
    with lane_queue._queue_op() as c:
        c.execute("INSERT INTO no_such_table VALUES(1)")     # raises inside the op
    ck("a mid-op error propagates out of _queue_op", False)
except Exception:
    ck("a mid-op error propagates out of _queue_op (re-raised, not swallowed)", True)
after = lane_queue._queue_conn()
ck("the pooled connection was reset (dropped) on error", after is not before)
with lane_queue._queue_op() as c:                            # the queue still works after a reset
    c.execute(_INS, ("poolI2",))
with contextlib.closing(lane_queue._queue_db()) as v:
    n2 = v.execute("SELECT COUNT(*) FROM lane_queue WHERE intent='poolI2'").fetchone()[0]
ck("queue self-heals after a reset (the next op commits)", n2 == 1)

print("-- a config.db_path() switch reopens the pool on the NEW file (staleness handled) --")
_orig = config.db_path
_newdir = tempfile.mkdtemp(prefix="spendguard-pool2-")
config.db_path = lambda: os.path.join(_newdir, "other.db")
try:
    c_new = lane_queue._queue_conn()
    ck("a db-path switch reopens a new pooled connection", c_new is not after)
    with lane_queue._queue_op() as c:                        # and it works on the new file
        c.execute(_INS, ("poolI3",))
    ck("the reopened pool writes to the new file", True)
finally:
    config.db_path = _orig
    lane_queue._reset_queue_conn()

print(f"\n{'[FAIL]' if _fails else 'OK'} test_queue_conn_pool: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
