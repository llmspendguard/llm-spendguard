"""Offline test for the price-literal audit (audit.main) — NO network. Scans a temp dir of fixture .py files.

audit.SCRIPTS is read at IMPORT time from $SPENDGUARD_AUDIT_DIR (else cwd), so we set that env var to a
fresh temp dir BEFORE importing the module. Covers:
  * clean dir -> exit 0, even with --ci.
  * a CORRECT canonical literal -> not flagged.
  * a WRONG keyed literal (gpt-5.5=(1.25,10)) -> flagged; main()==0 without --ci, ==1 with --ci.
  * a BANNED literal (old-Opus (15,75)) -> flagged.
  * the self-exclusion list (pricing.py et al. are skipped even when they contain wrong literals).
"""
import os
import sys
import tempfile

# Isolate SPENDGUARD_HOME before the venv sitecustomize loads the gate. Also pin the audit's scan dir HERE,
# before the re-exec, so the env carries through and audit.py picks it up at import time.
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.environ["SPENDGUARD_AUDIT_DIR"] = tempfile.mkdtemp(prefix="spendguard-audit-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

AUDIT_DIR = os.environ.get("SPENDGUARD_AUDIT_DIR") or tempfile.mkdtemp(prefix="spendguard-audit-")
os.environ.setdefault("SPENDGUARD_AUDIT_DIR", AUDIT_DIR)

from spendguard import audit                                        # noqa: E402

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def write(name, body):
    p = os.path.join(AUDIT_DIR, name)
    with open(p, "w") as f:
        f.write(body)
    return p


def clear_dir():
    import glob
    for p in glob.glob(os.path.join(AUDIT_DIR, "*.py")):
        os.remove(p)


# audit.SCRIPTS was frozen at import — confirm it points at our temp dir (so the test is hermetic).
print("-- audit scans the configured dir --")
check("SPENDGUARD_AUDIT_DIR honored at import", audit.SCRIPTS == AUDIT_DIR)

# ── clean dir -> no hits, exit 0 even with --ci ──────────────────────────────────────────────────────────────
print("-- clean dir --")
clear_dir()
write("clean_only.py", "X = 1\nname = 'gpt-4o'\n# nothing pricey here\n")
check("clean dir, plain run -> 0", audit.main([]) == 0)
check("clean dir, --ci -> 0", audit.main(["--ci"]) == 0)

# ── a CORRECT canonical literal is NOT flagged ───────────────────────────────────────────────────────────────
print("-- correct canonical literal --")
clear_dir()
write("correct_price.py", "PR = {'gpt-5.5': (5.0, 30.0)}   # canonical realtime\n")
check("correct gpt-5.5 (5.0,30.0) realtime -> not flagged (--ci 0)", audit.main(["--ci"]) == 0)
clear_dir()
write("correct_batch.py", "PR = {'gpt-5.5': (2.5, 15.0)}   # canonical batch\n")
check("correct gpt-5.5 (2.5,15.0) batch -> not flagged (--ci 0)", audit.main(["--ci"]) == 0)

# ── a WRONG keyed literal IS flagged ─────────────────────────────────────────────────────────────────────────
print("-- wrong keyed literal --")
clear_dir()
write("bad_price.py", "PR = {'gpt-5.5': (1.25, 10.0)}   # WRONG: priced as old gpt-5\n")
check("wrong gpt-5.5 (1.25,10) -> main([]) returns 0 (report-only, no --ci)", audit.main([]) == 0)
check("wrong gpt-5.5 (1.25,10) -> main(['--ci']) returns 1", audit.main(["--ci"]) == 1)

# ── gpt-5.5-pro keyed literal: wrong vs correct ──────────────────────────────────────────────────────────────
print("-- gpt-5.5-pro keyed literal --")
clear_dir()
write("bad_pro.py", "PR = {'gpt-5.5-pro': (5.0, 30.0)}   # WRONG for pro\n")
check("wrong gpt-5.5-pro -> --ci 1", audit.main(["--ci"]) == 1)
clear_dir()
write("good_pro.py", "PR = {'gpt-5.5-pro': (30.0, 180.0)}   # canonical pro realtime\n")
check("correct gpt-5.5-pro (30,180) -> --ci 0", audit.main(["--ci"]) == 0)

# ── a BANNED literal (regardless of dict shape) IS flagged ───────────────────────────────────────────────────
print("-- banned literals --")
clear_dir()
write("old_opus.py", "RATE = (15.0, 75.0)   # old-Opus rate, banned\n")
check("banned old-Opus (15,75) -> --ci 1", audit.main(["--ci"]) == 1)
clear_dir()
write("gpt55_out40.py", "P = {'gpt-5.5': {'out': 40}}   # banned out=40\n")
check("banned gpt-5.5 out=40 -> --ci 1", audit.main(["--ci"]) == 1)

# ── the audit excludes its own canonical/self-test files from scanning ───────────────────────────────────────
print("-- self-exclusion of canonical files --")
clear_dir()
write("pricing.py", "PR = {'gpt-5.5': (1.25, 10.0)}   # the canonical table legitimately shows examples\n")
write("audit.py", "RATE = (15.0, 75.0)\n")
write("reconcile_openai_spend.py", "PR = {'gpt-5.5': (1.25, 10.0)}\n")
check("excluded files NOT flagged even with wrong literals (--ci 0)", audit.main(["--ci"]) == 0)

# ── argv defaulting: main(None) reads sys.argv[1:] (no --ci there) ───────────────────────────────────────────
print("-- argv defaulting --")
clear_dir()
write("bad_default.py", "PR = {'gpt-5.5': (1.25, 10.0)}\n")
_saved_argv = sys.argv
sys.argv = ["audit"]                       # no --ci -> report-only path returns 0 even with a hit
try:
    rc = audit.main(None)
finally:
    sys.argv = _saved_argv
check("main(None) defaults to sys.argv[1:] (no --ci) -> 0 with a hit present", rc == 0)

print(f"\n{'[FAIL]' if failures else 'OK'} audit: {failures} failure(s)")
sys.exit(1 if failures else 0)
