"""budget.snapshot keeps only a SMALL number of local full-ledger recovery copies — the deep history is in B2.

Each snapshot is a copy of the WHOLE spend.db (~650MB in production), taken before a destructive reconcile/clear.
keep=20 × that size measured 12GB of local bloat in ~/.spendguard/snapshots (2026-09-15). Since B2 (spendguard-full,
daily) holds the deep history, local only needs the window since the last daily push: the default is now 4
(config safety.snapshot_keep), not 20. This guards: the default keep, an explicit keep, and that a pruned
snapshot's WAL/SHM companions are removed too (they used to be orphaned).

Offline + hermetic: a tiny fake ledger in a temp HOME; no network. Cleans up its temp HOME.
"""
import os
import sys
import atexit
import shutil
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-snapret-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
atexit.register(shutil.rmtree, os.environ["SPENDGUARD_HOME"], ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, config   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []

# a minimal but valid ledger db at config.db_path() — snapshot() copies it via the sqlite backup API
_con = sqlite3.connect(config.db_path())
_con.execute("CREATE TABLE IF NOT EXISTS t(x)")
_con.commit()
_con.close()
SNAP = config.HOME / "snapshots"


def _n_snaps():
    return len(list(SNAP.glob("spend-*.db")))


print("-- default keep is SMALL (4), not the old 20: 6 snapshots → 4 retained --")
for i in range(6):
    budget.snapshot(reason=f"ret-test-{i}", keep=None)     # keep=None → config safety.snapshot_keep (default 4)
fails += report_check("default retention keeps 4 (config safety.snapshot_keep), not 20", _n_snaps() == 4)

print("\n-- an explicit keep wins, and WAL/SHM companions of a pruned snapshot are removed too --")
# a stale snapshot that will sort FIRST (so keep=1 prunes it), with orphan WAL/SHM companions beside it
_stale = SNAP / "spend-00000000T000000Z-stale.db"
_stale.write_text("x")
(SNAP / "spend-00000000T000000Z-stale.db-wal").write_text("x")
(SNAP / "spend-00000000T000000Z-stale.db-shm").write_text("x")
budget.snapshot(reason="ret-final", keep=1)                # keep only the newest → prunes everything else
fails += report_check("explicit keep=1 retains exactly one snapshot", _n_snaps() == 1)
fails += report_check("the pruned snapshot's .db-wal companion was removed (not orphaned)",
                      not (SNAP / "spend-00000000T000000Z-stale.db-wal").exists())
fails += report_check("the pruned snapshot's .db-shm companion was removed (not orphaned)",
                      not (SNAP / "spend-00000000T000000Z-stale.db-shm").exists())

print(f"\n{'[FAIL]' if fails else 'OK'} test_snapshot_retention: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
