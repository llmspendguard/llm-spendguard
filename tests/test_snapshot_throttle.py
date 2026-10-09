"""budget.snapshot throttle — the fix for the measured snapshot churn: the scheduled reconcile's reattribute took a
FULL-DB copy on every run, so a backup landed ~every 45 min and keep=4 rotated through in ~3h. A ROUTINE mutation now
REUSES the newest snapshot within safety.snapshot_min_interval_hours (default 24 → one/day); a destructive op forces a
fresh one; keep bounds local copies to a day or two. Offline: a tiny real sqlite ledger in SPENDGUARD_HOME, no network."""
import os
import sqlite3
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-snap-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# a real (tiny) ledger db to back up
sqlite3.connect(config.db_path()).execute("CREATE TABLE IF NOT EXISTS t(x)").connection.commit()
SNAP = config.HOME / "snapshots"
def _n(): return len(list(SNAP.glob("spend-*.db")))

# ── 1. first snapshot creates a file ──
p1 = budget.snapshot(reason="first", keep=10)
ck("first snapshot creates a file", bool(p1) and os.path.exists(p1))
ck("exactly one snapshot on disk", _n() == 1)

# ── 2. a routine snapshot WITHIN the interval reuses the recent one — no new full-DB copy (the 45-min churn fix) ──
p2 = budget.snapshot(reason="routine-soon", keep=10)
ck("a routine snapshot within the interval creates NO new file", _n() == 1)
ck("...and returns the recent snapshot (a backup still protects the mutation)", p2 == p1)

# ── 3. a DESTRUCTIVE op forces a FRESH snapshot despite the recent one ──
time.sleep(1.1)                                      # distinct second-resolution stamp
p3 = budget.snapshot(reason="destructive", keep=10, force=True)
ck("force=True takes a fresh snapshot regardless of the interval", p3 != p1 and os.path.exists(p3) and _n() == 2)

# ── 4. after the interval elapses, a routine snapshot creates a new one ──
for f in SNAP.glob("spend-*.db"):                    # age every existing snapshot past the 24h default window
    _old = time.time() - 25 * 3600
    os.utime(f, (_old, _old))
time.sleep(1.1)
p4 = budget.snapshot(reason="after-gap", keep=10)
ck("after the interval, a routine snapshot creates a new file", p4 not in (p1, p3) and os.path.exists(p4))

# ── 5. keep bounds local copies (a day or two, not unbounded) ──
for f in SNAP.glob("spend-*.db"):
    f.unlink()
kept = []
for i in range(4):
    time.sleep(1.1)
    kept.append(budget.snapshot(reason=f"k{i}", keep=2, force=True))
ck("keep=2 retains only the 2 newest snapshots", _n() == 2)
ck("the retained ones are the 2 newest", all(os.path.exists(p) for p in kept[-2:]) and not any(os.path.exists(p) for p in kept[:-2]))

print(f"\n{'[FAIL]' if fails else 'OK'} test_snapshot_throttle: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
