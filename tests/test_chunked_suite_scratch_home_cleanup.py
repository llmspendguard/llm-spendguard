"""Offline guard: the chunked suite runner must REMOVE each per-file scratch SPENDGUARD_HOME when the file
finishes — pass, fail or timeout. NO db, NO network.

WHY. On 2026-09-14 the machine's disk hit 100% with 1.4GB free; $TMPDIR held 7,794 `sg-chunk-*` dirs
(29.9GB) left behind by suite runs, some 0.5GB each because the codex/claudecode state tests write a ~110MB
json plus its rotated backups into that home. A leak nothing measures is a leak that recurs, so this test
counts the scratch dirs before and after a real _run_one() and fails if the count grew.
"""
import glob
import importlib.util
import os
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


print("-- chunked_suite._run_one removes its scratch SPENDGUARD_HOME --")
runner = _load_runner()
before = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
rc, out, err = runner._run_one(SMALLEST_OFFLINE_TEST)
after = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
check("the spawned test file itself passed", rc == 0)
check("no new scratch home survives the run", not (after - before))

print("-- a path outside tests/ is refused, and leaves nothing behind either --")
before = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
rc, out, err = runner._run_one(RUNNER)                 # scripts/test/…, not tests/
after = _scratch_dirs(runner.SCRATCH_HOME_PREFIX)
check("refused with a non-zero code", rc != 0 and "not under" in err)
check("refusal created no scratch home", not (after - before))
print("done.")
