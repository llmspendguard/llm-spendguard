"""Guard for submit_storm's SYNC→ASYNC deadlock fix (Prompt-9 panel HIGH finding): a batch that hangs / exceeds the
collect window must surface as TYPED BACKPRESSURE within collect_timeout_s, NEVER an indefinite block of the caller
thread. Realtime rides the real governed path (xp_rate-paced); the batch executor here deliberately hangs so the
overflow cannot complete in time. Offline ($0): realtime via the FakeProvider wall; the hung batch is a gated Event.
"""
import os
import sys
import tempfile
import threading
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-storm-submit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = "300"     # rate 5/s → with horizon 1s a realtime share is served
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard import storm_submit  # noqa: E402

MODEL = "anthropic:claude-haiku-4-5"
N = 20
COLLECT_TIMEOUT_S = 1.5
fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


fp = H.FakeProvider(cap=1000, window_s=1.0).install()      # realtime wall (generous — not the thing under test here)
gate = threading.Event()


def _hang_batch(items):
    """A batch that does NOT finish within the collect window (simulates a multi-hour Batch-API ETA). Released at the
    end only, so the worker thread exits cleanly."""
    gate.wait(timeout=30)
    return {cid: {"text": "late %s" % H._extract_canary(p), "status_code": 200} for cid, p in items}


try:
    t0 = time.monotonic()
    res = storm_submit.submit_storm(
        [{"i": i} for i in range(N)], intent="acceptance:backpressure", model=MODEL, execute_batch=_hang_batch,
        deadline_s=1.0, collect_timeout_s=COLLECT_TIMEOUT_S, system="terse", reasoning="minimal",
        prompt_for=lambda t: "do %s" % H.canary(t["i"]), max_workers=8)
    elapsed = time.monotonic() - t0

    served = sum(1 for r in res if r.get("text") and not r.get("error"))
    backpressure = sum(1 for r in res if r.get("served_via") == "backpressure")
    # The contract, not a hand-picked count: EVERY item is either served (realtime) or typed-backpressured — no hang,
    # no silent drop — and BOTH paths are exercised (the realtime share ran; the hung overflow became backpressure).
    verify_condition("every item is served OR typed-backpressure — no dropped request, no hang", served + backpressure == N,
                     extra="served=%d backpressure=%d sum!=%d" % (served, backpressure, N))
    verify_condition("the realtime share WAS served (combo ran, not all-backpressure)", served >= 1, extra="served=%d" % served)
    verify_condition("the hung overflow became TYPED BACKPRESSURE (not a silent success, not a hang)", backpressure >= 1,
                     extra="backpressure=%d" % backpressure)
    # BOUNDED: a hang would block for the 1800s default (or forever); the fix returns within ~the collect window.
    verify_condition("BOUNDED: returned within the collect window, not blocked indefinitely",
                     elapsed < COLLECT_TIMEOUT_S + 5.0, extra="elapsed=%.2fs (vs 1800s default if it hung)" % elapsed)
finally:
    gate.set()                                             # release the hung worker so the process exits cleanly
    fp.uninstall()

print("\n%s: test_storm_submit — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)
