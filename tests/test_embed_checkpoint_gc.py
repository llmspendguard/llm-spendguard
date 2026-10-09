"""Retention guard for adapters.gc_embed_checkpoints — the bound that fixes the measured leak: 1,016 files / 19.65 GiB
of embed_<model>_<hash>.jsonl resume checkpoints in ~/.spendguard (1,014 created in one month) that embed() wrote per
multi-chunk run and nothing ever pruned. This test FAILS if the bound stops holding: cold files pruned by age; recent
files kept; oldest-first eviction once survivors exceed max_total_gb; keep_path never pruned; embed_batch_*.jsonl never
pruned (a submitted batch's request envelope); dry-run deletes nothing; a stat/unlink failure counted in `skipped`, not
masked as a clean sweep. Offline: files are stamped with os.utime; no embeddings client, no network, no spend."""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-embedgc-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

HOME = str(config.HOME)
def _mk(name, days_old, size):
    p = os.path.join(HOME, name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    t = time.time() - days_old * 86400.0
    os.utime(p, (t, t))
    return p
def _exists(name): return os.path.exists(os.path.join(HOME, name))
def _clear():
    for n in os.listdir(HOME):
        if n.startswith("embed_"):
            os.unlink(os.path.join(HOME, n))

# ── 1. AGE: cold (>max_age) pruned, recent kept; batch envelope + keep_path never touched; dry-run deletes nothing ──
_clear()
_mk("embed_m_old.jsonl", days_old=10, size=1000)       # COLD (>7d)
_mk("embed_m_recent.jsonl", days_old=1, size=1000)     # recent → kept
_mk("embed_batch_sub.jsonl", days_old=99, size=1000)   # a submitted batch envelope → NEVER pruned, even cold
keepp = _mk("embed_m_inflight.jsonl", days_old=99, size=1000)   # in-flight run's own checkpoint → NEVER pruned

r = adapters.gc_embed_checkpoints(max_age_days=7, max_total_gb=1000, apply=False, keep_path=keepp)
ck("age: the cold checkpoint is counted stale", r["stale"] == 1)
ck("age: examined excludes the batch envelope AND the keep_path (only old+recent)", r["examined"] == 2)
ck("age: dry-run deletes nothing on disk", _exists("embed_m_old.jsonl") and r["deleted"] == 0)
ck("age: bytes reflects only the doomed cold file", r["bytes"] == 1000)

r = adapters.gc_embed_checkpoints(max_age_days=7, max_total_gb=1000, apply=True, keep_path=keepp)
ck("age apply: the cold checkpoint is deleted", not _exists("embed_m_old.jsonl") and r["deleted"] == 1)
ck("age apply: the recent checkpoint is kept", _exists("embed_m_recent.jsonl"))
ck("age apply: the batch envelope is NEVER pruned (needed to collect the batch)", _exists("embed_batch_sub.jsonl"))
ck("age apply: keep_path (in-flight) is NEVER pruned even though it is cold", _exists("embed_m_inflight.jsonl"))

# ── 2. SIZE cap: oldest-first eviction once survivors exceed max_total_gb (all files recent, none cold by age) ──
_clear()
_mk("embed_m_a.jsonl", days_old=3, size=1000)          # oldest survivor → evicted first
_mk("embed_m_b.jsonl", days_old=2, size=1000)
_mk("embed_m_c.jsonl", days_old=1, size=1000)          # newest → kept
_cap_gb = 1500 / (1024 ** 3)                            # cap = 1500 bytes → 3×1000=3000 over it → evict oldest until <=
r = adapters.gc_embed_checkpoints(max_age_days=1000, max_total_gb=_cap_gb, apply=True)
ck("size: none cold by age (all recent)", r["stale"] == 0)
ck("size: evicted exactly enough to get under the cap (2 of 3 → 1000<=1500)", r["over_cap"] == 2 and r["deleted"] == 2)
ck("size: eviction was OLDEST-first (newest survives)", _exists("embed_m_c.jsonl"))
ck("size: the two oldest were the ones removed", not _exists("embed_m_a.jsonl") and not _exists("embed_m_b.jsonl"))

# ── 3. a stat/unlink failure is COUNTED in `skipped`, never masked as a clean sweep ──
_clear()
_mk("embed_m_doomed.jsonl", days_old=10, size=1000)    # cold → doomed
_orig_unlink = os.unlink
def _unlink_fail(p, *a, **k):
    if p.endswith("embed_m_doomed.jsonl"):
        raise OSError("simulated permission denied")
    return _orig_unlink(p, *a, **k)
os.unlink = _unlink_fail
try:
    r = adapters.gc_embed_checkpoints(max_age_days=7, apply=True)
finally:
    os.unlink = _orig_unlink
ck("failure: a doomed file that cannot be deleted is counted in `skipped`, not `deleted`",
   r["skipped"] == 1 and r["deleted"] == 0)
ck("failure: it is NOT masked as a clean sweep (the file is still there)", _exists("embed_m_doomed.jsonl"))
_orig_unlink(os.path.join(HOME, "embed_m_doomed.jsonl"))   # clean up

print(f"\n{'[FAIL]' if fails else 'OK'} test_embed_checkpoint_gc: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
