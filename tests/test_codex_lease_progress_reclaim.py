"""Offline unit guards for progress-heartbeat Codex lease reclamation."""
import pathlib
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import codex_daemon, config


LEASE_IDLE_TEST_S = 0.3
LEASE_MAX_TEST_S = 0.8
LEASE_PROGRESS_STEP_S = 0.05
LEASE_PROGRESS_RUN_S = 0.45
LEASE_WAIT_MARGIN_S = 0.2
LEASE_PROMPT = "lease-progress-guard"
LEASE_REPLY = {"text": "completed", "thread": "fixture-thread", "error": None}


class _LeaseProgressFakeWorker:
    def __init__(self, run_warm, activity_age):
        self.run_warm = run_warm
        self._activity_age = activity_age
        self._keep_warm = False
        self._environment_overrides = {}

    def seconds_since_last_activity(self):
        return self._activity_age()


def _lease_test_settings():
    return {"max_waiters": 0, "request_read_timeout_s": LEASE_IDLE_TEST_S,
            "recycle_on_timeout": True, "lease_idle_s": LEASE_IDLE_TEST_S,
            "lease_max_s": LEASE_MAX_TEST_S}


def _lease_test_pool(worker):
    pool_settings = {"size": 1, "isolate_homes": False, "health_interval_s": LEASE_MAX_TEST_S,
                     "respawn_backoff_s": LEASE_PROGRESS_STEP_S}
    with patch.object(config, "codex_daemon_pool_settings", return_value=pool_settings), \
            patch.object(config, "codex_daemon_admission_settings", return_value=_lease_test_settings()):
        return codex_daemon._CodexDaemonPool(lambda: worker, size=1)


def test_progressing_lease_completes_and_is_checked_in():
    progress_lock = threading.Lock()
    progress_at = [time.monotonic()]

    def activity_age():
        with progress_lock:
            return time.monotonic() - progress_at[0]

    def progressing_turn(**_kwargs):
        finish_at = time.monotonic() + LEASE_PROGRESS_RUN_S
        while time.monotonic() < finish_at:
            with progress_lock:
                progress_at[0] = time.monotonic()
            time.sleep(LEASE_PROGRESS_STEP_S)
        return dict(LEASE_REPLY)

    worker = _LeaseProgressFakeWorker(progressing_turn, activity_age)
    pool = _lease_test_pool(worker)
    with patch.object(config, "codex_daemon_admission_settings", return_value=_lease_test_settings()):
        result = pool.pool_run_warm(LEASE_PROMPT)

    assert result == LEASE_REPLY
    assert pool._idle_workers == [worker]


def test_silent_lease_is_reclaimed_and_retired():
    release_turn = threading.Event()

    def silent_turn(**_kwargs):
        release_turn.wait(LEASE_MAX_TEST_S + LEASE_WAIT_MARGIN_S)
        return dict(LEASE_REPLY)

    worker = _LeaseProgressFakeWorker(silent_turn, lambda: LEASE_IDLE_TEST_S + LEASE_PROGRESS_STEP_S)
    pool = _lease_test_pool(worker)
    recoveries = []
    with patch.object(config, "codex_daemon_admission_settings", return_value=_lease_test_settings()), \
            patch.object(pool, "_pool_start_recovery",
                         side_effect=lambda recovered, retire=False: recoveries.append((recovered, retire))):
        result = pool.pool_run_warm(LEASE_PROMPT)
    release_turn.set()

    assert result["error"] == "codex lease no-progress reclaim"
    assert recoveries == [(worker, True)]


def test_progressing_lease_is_reclaimed_at_absolute_backstop():
    release_turn = threading.Event()

    def runaway_turn(**_kwargs):
        release_turn.wait(LEASE_MAX_TEST_S + LEASE_WAIT_MARGIN_S)
        return dict(LEASE_REPLY)

    worker = _LeaseProgressFakeWorker(runaway_turn, lambda: LEASE_PROGRESS_STEP_S)
    pool = _lease_test_pool(worker)
    recoveries = []
    with patch.object(config, "codex_daemon_admission_settings", return_value=_lease_test_settings()), \
            patch.object(pool, "_pool_start_recovery",
                         side_effect=lambda recovered, retire=False: recoveries.append((recovered, retire))):
        result = pool.pool_run_warm(LEASE_PROMPT)
    release_turn.set()

    assert result["error"] == "codex lease absolute backstop expired"
    assert recoveries == [(worker, True)]
