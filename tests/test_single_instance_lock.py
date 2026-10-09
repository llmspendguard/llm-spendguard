"""config.single_instance_lock is a NON-BLOCKING, cross-process count-1 semaphore (mutex): the FIRST holder proceeds,
a SECOND is refused so it cannot start a duplicate. This guards `spendguard deploy` — two concurrent gate runs each
spawn a full test suite, starve each other's CPU, and flake the receipt/timeout-sensitive tests (measured 2026-10-09,
two accidental `deploy` runs raced). flock is per-open-file-description, so a second os.open in the SAME process still
contends — the contention is real, not a same-process artefact. Offline, isolated HOME, no network, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-silock-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

NAME = "test_deploy_singleton"

# ── 1. a single acquire holds the lock and proceeds ──
with config.single_instance_lock(NAME) as got_outer:
    ck("first acquire yields True (this process holds the mutex)", got_outer is True)

    # ── 2. a SECOND acquire WHILE the first is held is refused (does not start a duplicate) ──
    with config.single_instance_lock(NAME) as got_inner:
        ck("second acquire WHILE held yields False (duplicate refused)", got_inner is False)

# ── 3. once the first `with` exits, the lock is released — a fresh acquire succeeds again ──
with config.single_instance_lock(NAME) as got_again:
    ck("acquire after release yields True (OS dropped the flock on fd close)", got_again is True)

# ── 4. release happens even if the body RAISES (finally drops the fd) ──
class _Boom(Exception):
    pass
try:
    with config.single_instance_lock(NAME):
        raise _Boom()
except _Boom:
    pass
with config.single_instance_lock(NAME) as got_post_raise:
    ck("acquire after an exception in the body yields True (lock released in finally)", got_post_raise is True)

# ── 5. the lockfile lives under HOME with the expected name (parsing a fixed path, not deciding meaning) ──
ck("lockfile path is HOME/<name>.lock",
   os.path.exists(os.path.join(os.environ["SPENDGUARD_HOME"], f"{NAME}.lock")))

print(f"\n{'[FAIL]' if fails else 'OK'} test_single_instance_lock: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
