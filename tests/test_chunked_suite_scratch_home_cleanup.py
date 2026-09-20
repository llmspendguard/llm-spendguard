"""Offline guard: the chunked suite runner must REMOVE each per-file scratch SPENDGUARD_HOME when the file
finishes — pass, fail or timeout. NO db, NO network.

WHY. On 2026-09-14 the machine's disk hit 100% with 1.4GB free; $TMPDIR held 7,794 `sg-chunk-*` dirs
(29.9GB) left behind by suite runs, some 0.5GB each because the codex/claudecode state tests write a ~110MB
json plus its rotated backups into that home. A leak nothing measures is a leak that recurs, so this test
counts the scratch dirs before and after a real _run_one() and fails if the count grew.

CONCURRENCY. The count is scoped to a PRIVATE tempdir this test owns (`tempfile.tempdir` = an isolated root),
NOT the shared $TMPDIR. Otherwise another process creating an sg-chunk-* home during the window — a concurrent
`spendguard deploy`, or another Claude session's spendguard call — lands in (after - before) and is misread as
a leak THIS test caused (observed: the full-gate run intermittently red on exactly this assertion while other
spendguard servers were active). Isolation makes the diff see ONLY dirs this test's _run_one created, so the
guard measures _run_one, not global machine state — while STILL failing on a real leak (proven below, so the
isolation can never quietly turn into "always passes").
"""
import glob
import importlib.util
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(os.path.dirname(HERE), "scripts", "test", "chunked_suite.py")
SMALLEST_OFFLINE_TEST = os.path.join(HERE, "test_cacheaudit.py")     # pure logic, runs in well under a second


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


def _load_runner():
    spec = importlib.util.spec_from_file_location("chunked_suite_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scratch_dirs(prefix):
    return set(glob.glob(os.path.join(tempfile.gettempdir(), prefix + "*")))


runner = _load_runner()

# CONCURRENCY ISOLATION (see module docstring): steer tempfile — the SAME module object this test and the runner
# both import — at a private root, so `_run_one`'s scratch mkdtemp AND our before/after glob live ONLY here. A
# concurrent process's sg-chunk-* in the real $TMPDIR can no longer be counted as this test's leak. `_iso` itself
# is created in the real $TMPDIR first (its own distinct prefix), then becomes the base for everything after.
_iso = tempfile.mkdtemp(prefix="sg-cleanup-iso-")
tempfile.tempdir = _iso

print("-- chunked_suite._run_one removes its scratch SPENDGUARD_HOME (clean run) --")
before = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
rc, out, err = runner._run_one(SMALLEST_OFFLINE_TEST)
after = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
check("the spawned test file itself passed", rc == 0)
check("no new scratch home survives the run", not (after - before))

print("-- the guard STILL catches a real leak: with cleanup disabled, the scratch home survives --")
_real_rmtree = runner.shutil.rmtree
runner.shutil.rmtree = lambda *a, **k: None          # simulate _run_one failing to remove its own scratch home
leaked = set()
try:
    before = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
    rc, out, err = runner._run_one(SMALLEST_OFFLINE_TEST)
    leaked = _scratch_dirs(runner.SCRATCH_HOME_PREFIX) - before
    check("a genuinely-unremoved scratch home IS detected (after - before is non-empty)", bool(leaked))
finally:
    runner.shutil.rmtree = _real_rmtree
    for d in leaked:                                  # clean up the deliberately-leaked dir(s) ourselves
        _real_rmtree(d, ignore_errors=True)

print("-- a path outside tests/ is refused, and leaves nothing behind either --")
before = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
rc, out, err = runner._run_one(RUNNER)                 # scripts/test/…, not tests/
after = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
check("refused with a non-zero code", rc != 0 and "not under" in err)
check("refusal created no scratch home", not (after - before))

shutil.rmtree(_iso, ignore_errors=True)               # tidy our own isolated root
print("done.")
