"""lock → write → unlock: no ledger operation may leave the sqlite WRITE LOCK held.

WHY, MEASURED. A `conversation_digest --watch` daemon (ccwatch) ran for 31 days holding a spendguard connection
whose write transaction was never released — so its spend.db write lock stayed held and its WAL grew to 36 MB,
uncheckpointed. Every OTHER writer then got sqlite `database is locked`: a commit hook's own gated LLM call, and
a plain `budget.record`, both failed — and POST-CUTOVER a failed write DROPS the charge (spend_events is the
sole money-of-record; there is no `charges` fallback to catch it). The holder was stale pre-cutover code; the
current writers commit per call and release. This test PINS that discipline so it cannot silently regress:

  a long-lived process may keep a connection OPEN indefinitely, but never a TRANSACTION — every read and every
  write must leave the write lock FREE for the next writer (lock, write, unlock — atomically).

The check is a SECOND connection to the same on-disk file: if it can `BEGIN IMMEDIATE` right after each op, no
one is holding the write lock. Cross-connection locking is invisible to an in-memory db, so this uses a real
file in an isolated home.
"""
import os, sys, tempfile, sqlite3, io, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lock-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, budget

config.budget_backend = lambda: "sqlite"          # the cross-process ledger writes to the on-disk db (not memory)

fails = 0
def ck(label, cond):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

DB = config.db_path()
_probe = sqlite3.connect(DB, timeout=1)           # stands in for 'another writer / another process'


def writer_can_acquire():
    """True iff a DIFFERENT connection can take the write lock right now — i.e. nobody is holding it idle."""
    try:
        _probe.execute("BEGIN IMMEDIATE")
        _probe.rollback()
        return True
    except sqlite3.OperationalError:
        return False


L = budget._ledger()
ck("opening the ledger connection (schema ensure incl.) holds NO write lock", writer_can_acquire())

L.spent_dec(since="2026-08-01")
ck("a READ (spent_dec) leaves the write lock free — WAL readers never block writers", writer_can_acquire())
ck("...and leaves the connection OUT of any transaction", not L._conn.in_transaction)

list(L.query(since="2026-08-01"))
ck("a query() leaves the write lock free", writer_can_acquire())

_buf = io.StringIO()
with contextlib.redirect_stderr(_buf):
    budget.record("anthropic", "claude-haiku-4-5", "realtime", 0.01, project="lock-test")
ck("a WRITE commits and RELEASES the lock (lock → write → unlock)", writer_can_acquire())
ck("...the write actually landed (no fail-open warning)", _buf.getvalue().strip() == "")
ck("...the ledger connection is left OUT of any transaction", not L._conn.in_transaction)

# an UPDATE is multi-statement (mutation + chained audit) — it must still release when done
_row = next(iter(L.query(since="2026-08-01")), None)
if _row is not None:
    L.update(_row["id"], {"intent": "lock-test-update"}, actor="lock-test", reason="release check")
ck("an UPDATE (mutation + audit) releases the lock", writer_can_acquire())
ck("...and leaves no open transaction behind", not L._conn.in_transaction)

# a bulk() load defers per-row commits, so it MAY hold the lock inside the block — but on exit it commits and
# must release. (Inside the block the lock being held is correct: that is an active transaction, not idle.)
with L.bulk():
    budget.record("openai", "gpt-5.5", "realtime", 0.02, project="lock-test")
ck("a bulk() context RELEASES the lock on exit (commit)", writer_can_acquire())
ck("...and leaves no open transaction behind", not L._conn.in_transaction)

print(f"\n{'[FAIL]' if fails else 'OK'} test_ledger_releases_write_lock: {fails} failure(s)")
sys.exit(1 if fails else 0)
