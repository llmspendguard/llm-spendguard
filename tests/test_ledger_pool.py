"""Guard — the SHARED ledger connection pool in config (config.pooled_ledger_conn / ledger_op / reset_ledger_conn /
fresh_ledger_conn), which every subsystem's _X_db() now delegates to (calls/callio/bulkgate/learn/semcache/lane_queue).
Pins the contract that consolidating 8 bespoke singletons onto ONE layer must never lose:
  · REUSE — same key on a thread returns the SAME connection;
  · KEYED ISOLATION — different subsystem keys get DIFFERENT connections (so a nested op can't commit/rollback
    another subsystem's transaction);
  · ledger_op COMMITS on success (a write is visible to a fresh connection) and ROLLS BACK + DROPS the pooled
    connection on error (self-heal), re-raising;
  · reset_ledger_conn drops ONE key's connection, leaving others intact;
  · fresh_ledger_conn is a FRESH connection (never the pooled one), so closing it can't corrupt the pool;
  · the after-fork handler clears the whole pool (a forked child must reopen, never reuse the parent's fds).
Hermetic: pure sqlite through the pool; no lanes, no network."""
import os
import sys
import contextlib
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-ledgerpool-"))

from spendguard import config

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

def _noschema(c):
    pass

def _ens_tpool(c):
    c.execute("CREATE TABLE IF NOT EXISTS t_pool(x INTEGER)")
    c.commit()

print("-- reuse + keyed isolation --")
ca = config.pooled_ledger_conn("subA", _noschema)
cb = config.pooled_ledger_conn("subB", _noschema)
ck("same key reused (same object)", config.pooled_ledger_conn("subA", _noschema) is ca)
ck("different keys → different connections", ca is not cb)

print("-- ledger_op commits on success (visible to a fresh connection) --")
with config.ledger_op("subC", _ens_tpool) as c:
    c.execute("INSERT INTO t_pool VALUES (1)")
with contextlib.closing(config.fresh_ledger_conn(_ens_tpool)) as v:
    n = v.execute("SELECT COUNT(*) FROM t_pool WHERE x=1").fetchone()[0]
ck("ledger_op committed the write", n == 1)

print("-- ledger_op rolls back + DROPS the pooled connection on error, re-raising --")
before = config.pooled_ledger_conn("subC", _ens_tpool)
try:
    with config.ledger_op("subC", _ens_tpool) as c:
        c.execute("INSERT INTO no_such_table VALUES (1)")
    ck("error propagated", False)
except Exception:
    ck("ledger_op re-raises on error", True)
after = config.pooled_ledger_conn("subC", _ens_tpool)
ck("the pooled connection was dropped on error", after is not before)
with config.ledger_op("subC", _ens_tpool) as c:                # self-heals
    c.execute("INSERT INTO t_pool VALUES (2)")
ck("the pool self-heals after a reset", True)

print("-- reset_ledger_conn drops ONE key, leaves others intact --")
x1 = config.pooled_ledger_conn("subD", _noschema)
config.reset_ledger_conn("subD")
ck("reset drops that key's connection", config.pooled_ledger_conn("subD", _noschema) is not x1)
ck("reset of one key leaves others intact", config.pooled_ledger_conn("subA", _noschema) is ca)

print("-- fresh_ledger_conn is not the pooled connection --")
fr = config.fresh_ledger_conn(_noschema)
ck("fresh_ledger_conn is a separate connection", fr is not ca)
fr.close()
ck("closing the fresh conn leaves the pool intact", config.pooled_ledger_conn("subA", _noschema) is ca)

print("-- the after-fork handler clears the whole pool --")
config._reset_ledger_pool_after_fork()
ck("after-fork clears the pool (subA reopens fresh)", config.pooled_ledger_conn("subA", _noschema) is not ca)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_ledger_pool: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
