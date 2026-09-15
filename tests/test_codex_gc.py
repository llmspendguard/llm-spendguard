"""codex_exec.gc_shell_snapshots — prune codex CLI's shell-snapshot residue by AGE, safely.

codex writes one .sh per `codex exec` into <codex_home>/shell_snapshots and never cleans it; spendguard's codex
lane drives that heavily (measured 7.2GB / 38,739 files). This guards: dry-run (default) deletes nothing; apply
deletes ONLY snapshots older than the cutoff; a recent snapshot is NEVER touched (an active session may hold it);
re-running is idempotent; a missing dir is a true empty (examined=0, error=None), not a masked failure.

Offline + hermetic: a temp CODEX_HOME with hand-aged files; no codex CLI, no network. Cleans up.
"""
import os
import sys
import time
import atexit
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="sg-codexgc-")
os.environ["CODEX_HOME"] = _HOME
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []
snapdir = os.path.join(_HOME, "shell_snapshots")
os.makedirs(snapdir)
_old = time.time() - 10 * 86400          # 10 days old → stale (cutoff 7d)
for n in ("old-a.sh", "old-b.sh"):
    p = os.path.join(snapdir, n)
    with open(p, "w") as f:
        f.write("x" * 100)
    os.utime(p, (_old, _old))
_recent = os.path.join(snapdir, "recent.sh")
with open(_recent, "w") as f:
    f.write("y" * 50)                    # fresh mtime → must be preserved

print("-- DRY-RUN (default): reports stale, deletes NOTHING --")
r = codex_exec.gc_shell_snapshots(max_age_days=7, apply=False)
fails += report_check("dry-run: examined 3, stale 2, deleted 0, error None",
                      r["examined"] == 3 and r["stale"] == 2 and r["deleted"] == 0 and r["error"] is None)
fails += report_check("dry-run left all 3 files on disk", len(os.listdir(snapdir)) == 3)

print("\n-- APPLY: deletes ONLY the stale ones; the recent snapshot is preserved --")
r2 = codex_exec.gc_shell_snapshots(max_age_days=7, apply=True)
fails += report_check("apply deleted the 2 stale snapshots", r2["deleted"] == 2 and r2["skipped"] == 0)
fails += report_check("the RECENT snapshot is preserved (age-based never touches recent)",
                      os.path.exists(_recent) and not os.path.exists(os.path.join(snapdir, "old-a.sh")))

print("\n-- idempotent: a second apply finds nothing stale --")
r3 = codex_exec.gc_shell_snapshots(max_age_days=7, apply=True)
fails += report_check("re-run: stale 0, deleted 0", r3["stale"] == 0 and r3["deleted"] == 0)

print("\n-- a missing shell_snapshots dir is a TRUE empty (examined 0, error None), not a masked failure --")
os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="sg-codexgc-empty-")
atexit.register(shutil.rmtree, os.environ["CODEX_HOME"], ignore_errors=True)
r4 = codex_exec.gc_shell_snapshots(max_age_days=7, apply=True)
fails += report_check("no dir → examined 0, error None (distinguishable from a failed scan)",
                      r4["examined"] == 0 and r4["error"] is None and r4["deleted"] == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_gc: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
