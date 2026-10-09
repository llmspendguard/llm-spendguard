"""lane_queue lives in its OWN database, not the spend.db money ledger — the architectural fix for the measured
runaway (2026-10-08): lane_queue was 2.76GB = 63% of a 4.4GB ledger, so the drain's purge scanned multi-GB under the
ledger write lock every cycle, bloated every snapshot/backup, and starved the WAL checkpoint. This locks in:
  • lane_queue_db_path() is a DIFFERENT file from db_path(); enqueue writes to it, NOT spend.db,
  • purge() deletes in bounded chunks (short txns) with the (state, updated_ts) index, and bounds the archive,
  • purge_due() decouples purge from the drain cycle (not every call),
  • the drain takes a single-instance flock (a second drain is refused, not run concurrently),
  • migrate_from_ledger() moves NON-terminal rows out of spend.db and drops the old table (idempotent).
Offline, isolated HOME, no network, no LLM."""
import os
import sys
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lq-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue, config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

def _has_table(path, name="lane_queue"):
    if not os.path.exists(path):
        return False
    c = sqlite3.connect(path)
    try:
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    finally:
        c.close()

# ── 1. the queue has its OWN file, separate from the ledger ──
ck("lane_queue_db_path() != db_path() (queue is NOT the money ledger)", config.lane_queue_db_path() != config.db_path())
ck("the queue db basename is lane_queue.db", os.path.basename(config.lane_queue_db_path()) == "lane_queue.db")

# ── 2. enqueue writes to lane_queue.db, NOT spend.db ──
lane_queue.enqueue_many("probe:own-db", ["task one", "task two", "task three"], priority=0)
ck("enqueue created the lane_queue table in lane_queue.db", _has_table(config.lane_queue_db_path()))
ck("enqueue did NOT create lane_queue in the spend.db ledger",
   not _has_table(config.db_path()))
ck("queue depth sees the 3 pending rows", lane_queue.queue_depth().get("pending") == 3)

# ── 3. the (state, updated_ts) purge index exists ──
_c = sqlite3.connect(config.lane_queue_db_path())
_idx = [r[0] for r in _c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='lane_queue'")]
_c.close()
ck("the purge index (state, updated_ts) was created", "lane_queue_purge" in _idx)

# ── 4. purge is chunked + bounded, and decoupled from the cycle ──
#     insert OLD terminal rows directly, then purge in small chunks and assert all are archived+deleted.
import datetime  # noqa: E402
old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
with lane_queue._queue_op() as c:
    c.executemany("INSERT INTO lane_queue(intent,task,state,created_ts,updated_ts) VALUES(?,?,?,?,?)",
                  [("probe:old", "t%d" % i, "done" if i % 2 else "failed", old_ts, old_ts) for i in range(25)])
arch = os.path.join(str(config.HOME), "lane_queue_archive.jsonl")
res = lane_queue.purge(retain_days=7, archive_path=arch, chunk=10)   # 25 terminal rows, chunk=10 → 3 chunks
ck("purge archived+deleted all 25 old terminal rows (chunked)", res.get("archived") == 25 and res.get("deleted") == 25)
ck("the archive jsonl was written", os.path.exists(arch))
ck("the 3 live pending rows were NOT purged", lane_queue.queue_depth().get("pending") == 3)
ck("purge_due() is False right after a purge (decoupled, not every cycle)", lane_queue.purge_due() is False)
ck("purge_due(0) is always True (0 = every cycle, the old behaviour)", lane_queue.purge_due(min_interval_s=0) is True)

# ── 5. archive rotation bounds the append-only log ──
with open(arch, "ab") as f:
    f.write(b"x" * (2 * 1024 * 1024))                 # pad past a tiny cap
lane_queue._bound_archive(arch, max_mb=1)             # cap 1MB → rotate
ck("over-cap archive rotated to .1 (bounded, not unbounded growth)", os.path.exists(arch + ".1"))

# ── 6. single-instance drain lock ──
fd = lane_queue._acquire_drain_lock()
ck("drain lock acquired", fd is not None and fd != -1)
fd2 = lane_queue._acquire_drain_lock()
ck("a SECOND concurrent drain lock is refused (None) — no overlap", fd2 is None)
lane_queue._release_drain_lock(fd)
fd3 = lane_queue._acquire_drain_lock()
ck("after release the lock can be re-acquired", fd3 is not None and fd3 != -1)
lane_queue._release_drain_lock(fd3)

# ── 7. migrate_from_ledger: move live rows out of a simulated old ledger table, drop it ──
#     simulate the pre-migration state: a lane_queue table INSIDE spend.db with live + terminal rows.
led = config.db_path()
lc = sqlite3.connect(led)
lane_queue._ensure_queue_schema(lc)                   # same schema in the ledger (as the old co-located table was)
lc.executemany("INSERT INTO lane_queue(intent,task,state,created_ts,updated_ts) VALUES(?,?,?,?,?)",
               [("mig", "live1", "pending", old_ts, old_ts),
                ("mig", "live2", "leased", old_ts, old_ts),
                ("mig", "terminal", "done", old_ts, old_ts)])
lc.commit()
lc.close()
def _nonterminal():
    d = lane_queue.queue_depth()
    return d.get("pending", 0) + d.get("leased", 0)
before = _nonterminal()
mig = lane_queue.migrate_from_ledger()
ck("migration moved the 2 NON-terminal rows (not the terminal one)", mig.get("moved") == 2)
ck("migration dropped lane_queue from the spend.db ledger", mig.get("dropped") is True and not _has_table(led))
ck("the 2 live rows (1 pending + 1 leased) now appear in the queue's own db", _nonterminal() == before + 2)
ck("migration is idempotent (2nd run is a no-op)", lane_queue.migrate_from_ledger().get("moved") == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_queue_own_db: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
