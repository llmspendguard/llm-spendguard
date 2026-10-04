"""GUARD for the lane_queue additive-migration race (found in the 429-storm replay: "durable enqueue FAILED
(OperationalError: duplicate column name: sla_class)"). The old _ensure_queue_schema did a check-then-ALTER
(PRAGMA table_info, then conditional ADD COLUMN) which is TOCTOU across the concurrent pooled connections: under a
fan, two connections both pass the check on a fresh table, the second ALTER raises "duplicate column name", and
enqueue swallowed it to [] — i.e. the durable record was SILENTLY LOST exactly when a storm is being queued.

This test forces maximum overlap with a barrier: N threads each create their own pooled queue connection (which runs
schema-ensure) and enqueue at the same instant onto ONE fresh db. The invariant: EVERY enqueue returns a row id and
the queue holds N rows — no row lost to a schema race. Offline, $0.
"""
import os
import sys
import tempfile
import threading

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lqrace-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import lane_queue  # noqa: E402

N = 24
ROUNDS = 6                                               # each round starts from a FRESH table so the ALTER race re-fires
fails = []


def _drop_table_fresh():
    """Drop lane_queue via a one-off connection so the next round's connections re-CREATE + re-ALTER (re-triggering
    the additive-migration race on a column-less table)."""
    db = lane_queue._queue_db()
    try:
        db.execute("DROP TABLE IF EXISTS lane_queue")
        db.commit()
    finally:
        db.close()


for rnd in range(ROUNDS):
    _drop_table_fresh()
    lane_queue._reset_queue_conn()                       # this (main) thread's conn is stale after the drop
    intent = "schema-race-%d" % rnd
    barrier = threading.Barrier(N)
    ids = [None] * N
    errors = []

    def _worker(i, _intent=intent, _ids=ids, _errs=errors, _bar=barrier):
        try:
            _bar.wait()                                  # all N threads hit schema-ensure + enqueue at the same instant
            _ids[i] = lane_queue.enqueue(_intent, "task-%d" % i, sla_class="realtime")
        except Exception as e:                           # enqueue should degrade to [], never raise
            _errs.append("%r" % e)

    threads = [threading.Thread(target=_worker, args=(i,), name="lqrace-%d-%d" % (rnd, i)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    enqueued = sum(1 for x in ids if x is not None)
    rows = 0
    try:
        lane_queue._reset_queue_conn()
        with lane_queue._queue_op() as c:
            rows = c.execute("SELECT COUNT(*) FROM lane_queue WHERE intent=?", (intent,)).fetchone()[0]
    except Exception as e:
        errors.append("count failed: %r" % e)

    if errors:
        fails.append("round %d: raised (should degrade, not raise): %s" % (rnd, errors[:2]))
    if enqueued != N:
        fails.append("round %d: only %d/%d enqueue() returned an id (race dropped %d)" % (rnd, enqueued, N, N - enqueued))
    if rows != N:
        fails.append("round %d: only %d/%d rows landed durably (race silently lost %d)" % (rnd, rows, N, N - rows))

if fails:
    print("RED: test_lane_queue_schema_race")
    for f in fails:
        print("   RED:", f)
    sys.exit(1)
print("ALL GREEN: test_lane_queue_schema_race — %d rounds x %d threads, every row durable, 0 errors" % (ROUNDS, N))
sys.exit(0)
