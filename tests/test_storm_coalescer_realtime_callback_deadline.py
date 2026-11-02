"""A hung injected realtime callback cannot wedge StormCoalescer's coordinator.

The child process is intentional: the callback blocks forever, so process containment is the only honest cleanup.
Both the in-child faulthandler and the parent subprocess timeout derive from the same realtime horizon exercised by
the coalescer. Removing the bounded ``Future.result`` therefore fails by a bounded watchdog instead of hanging CI.
"""
import contextlib
import faulthandler
import io
import os
import subprocess
import sys
import threading
import time


REALTIME_HORIZON_S = 0.1
COORDINATOR_RETURN_MULTIPLIER = 4
TRACEBACK_WATCHDOG_MULTIPLIER = 8
SUBPROCESS_WATCHDOG_MULTIPLIER = 20
CHILD_MODE_ENV = "SPENDGUARD_STORM_REALTIME_DEADLINE_CHILD"
TEST_PROVIDER = "deadline-test-provider"
EXPECTED_TIMEOUT_COUNT = 1


def run_hung_realtime_deadline_child():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo_root, "src"))
    from spendguard.storm_coalescer import StormCoalescer, _Pending

    callback_blocker = threading.Event()

    def execute_realtime_that_never_returns(_request):
        callback_blocker.wait()

    coalescer = StormCoalescer(
        execute_realtime=execute_realtime_that_never_returns,
        execute_batch=lambda _requests: {},
        sustainable_rate_per_s=1.0 / REALTIME_HORIZON_S,
        horizon_s=REALTIME_HORIZON_S,
        provider=TEST_PROVIDER,
    )
    pending = _Pending("hung realtime request", EXPECTED_TIMEOUT_COUNT)
    surfaced_stderr = io.StringIO()
    faulthandler.dump_traceback_later(REALTIME_HORIZON_S * TRACEBACK_WATCHDOG_MULTIPLIER, exit=True)
    started_at = time.monotonic()
    with contextlib.redirect_stderr(surfaced_stderr):
        rerouted = coalescer._run_realtime_paced([pending])
    elapsed_s = time.monotonic() - started_at
    faulthandler.cancel_dump_traceback_later()

    surfaced = surfaced_stderr.getvalue()
    assert elapsed_s < REALTIME_HORIZON_S * COORDINATOR_RETURN_MULTIPLIER, elapsed_s
    assert rerouted == [pending]
    assert coalescer.realtime_timeout_rerouted == EXPECTED_TIMEOUT_COUNT
    assert TEST_PROVIDER in surfaced
    assert str(EXPECTED_TIMEOUT_COUNT) in surfaced
    print("PASS elapsed=%.3fs rerouted=%d surfaced=%r" %
          (elapsed_s, coalescer.realtime_timeout_rerouted, surfaced.strip()), flush=True)
    os._exit(0)  # the deliberately blocked callback is process-contained and must never be released by the test


def run_realtime_deadline_subprocess_watchdog():
    child_environment = dict(os.environ)
    child_environment[CHILD_MODE_ENV] = "1"
    repo_source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    existing_pythonpath = child_environment.get("PYTHONPATH")
    child_environment["PYTHONPATH"] = (repo_source + os.pathsep + existing_pythonpath
                                       if existing_pythonpath else repo_source)
    completed = subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        capture_output=True,
        text=True,
        env=child_environment,
        timeout=REALTIME_HORIZON_S * SUBPROCESS_WATCHDOG_MULTIPLIER,
    )
    assert completed.returncode == 0, "stdout=%s\nstderr=%s" % (completed.stdout, completed.stderr)
    assert "PASS" in completed.stdout, completed.stdout
    print(completed.stdout.strip())


if __name__ == "__main__":
    if os.environ.get(CHILD_MODE_ENV):
        run_hung_realtime_deadline_child()
    else:
        run_realtime_deadline_subprocess_watchdog()
