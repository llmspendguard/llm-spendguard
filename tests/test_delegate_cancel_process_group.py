"""Deterministic guards for cancellation's bounded whole-session reap."""
import errno
import functools
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

PROCESS_GROUP_TEST_HOME = Path(align_test_startup_home("sg-delegate-group-reap-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import process_group_reaper  # noqa: E402

PROCESS_GROUP_TEST_TERM_GRACE_S = 0.2
PROCESS_GROUP_TEST_KILL_GRACE_S = 2.0
PROCESS_GROUP_TEST_POLL_S = 0.01
PROCESS_GROUP_TEST_WARNING_CONTEXT = "delegate cancellation regression guard"
PROCESS_GROUP_TEST_DIRECTORY_PREFIX = "sg-cancel-group-reap-"
PROCESS_GROUP_TEST_WRITER_FILENAME_PREFIX = "writer-"
PROCESS_GROUP_TEST_MUTATION_ENV = "SPENDGUARD_TEST_GROUP_REAP_MUTATION"
PROCESS_GROUP_TEST_ONLY_TIMEOUT_ENV = "SPENDGUARD_TEST_GROUP_REAP_ONLY_TIMEOUT"
PROCESS_GROUP_TEST_WORKER_ONLY_MUTATION = "worker-only"
PROCESS_GROUP_TEST_REPOSITORY_DIRECTORY_NAME = "repository"
PROCESS_GROUP_TEST_ENABLED_VALUE = "1"
PROCESS_GROUP_TEST_WRITER_CODE = """
import pathlib,signal,sys,time
target=pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
counter=0
while True:
    (target / (sys.argv[2] + str(counter))).write_text(str(counter))
    counter += 1
    time.sleep(float(sys.argv[3]))
"""
PROCESS_GROUP_TEST_LEADER_CODE = """
import signal,subprocess,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child=subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]])
print(child.pid, flush=True)
while True: time.sleep(float(sys.argv[4]))
"""
PROCESS_GROUP_TEST_EPERM_SKIP_REASON = (
    "SKIP/UNREVIEWED: EPERM denies killpg to a foreign session group; real process-group reap is verified "
    "only on permissive hosts (CI/Linux)")


def discard_process_group_reap_warning(_message):
    """Accept an unexpected reaper warning where the surrounding assertion owns the verdict."""


@functools.cache
def host_permits_foreign_session_killpg():
    """Return whether this host permits liveness probes against a foreign session group."""
    with tempfile.TemporaryDirectory(prefix=PROCESS_GROUP_TEST_DIRECTORY_PREFIX) as temporary:
        repository = Path(temporary) / PROCESS_GROUP_TEST_REPOSITORY_DIRECTORY_NAME
        repository.mkdir()
        leader = subprocess.Popen(
            [sys.executable, "-c", PROCESS_GROUP_TEST_LEADER_CODE, PROCESS_GROUP_TEST_WRITER_CODE,
             str(repository), PROCESS_GROUP_TEST_WRITER_FILENAME_PREFIX, str(PROCESS_GROUP_TEST_POLL_S)],
            start_new_session=True, stdout=subprocess.PIPE, text=True)
        descendant_pid = int(leader.stdout.readline())
        try:
            try:
                os.killpg(leader.pid, process_group_reaper.PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
            except PermissionError:
                return False
            return process_group_reaper.reap_process_group_bounded(
                leader.pid, terminate_grace_s=PROCESS_GROUP_TEST_TERM_GRACE_S,
                force_kill_grace_s=PROCESS_GROUP_TEST_KILL_GRACE_S,
                poll_interval_s=PROCESS_GROUP_TEST_POLL_S,
                warning_context=PROCESS_GROUP_TEST_WARNING_CONTEXT,
                emit_warning=discard_process_group_reap_warning)
        finally:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            leader.wait(timeout=PROCESS_GROUP_TEST_KILL_GRACE_S)


def assert_cancel_group_guard_reaps_late_descendant():
    with tempfile.TemporaryDirectory(prefix=PROCESS_GROUP_TEST_DIRECTORY_PREFIX) as temporary:
        repository = Path(temporary) / PROCESS_GROUP_TEST_REPOSITORY_DIRECTORY_NAME
        repository.mkdir()
        leader = subprocess.Popen(
            [sys.executable, "-c", PROCESS_GROUP_TEST_LEADER_CODE, PROCESS_GROUP_TEST_WRITER_CODE,
             str(repository), PROCESS_GROUP_TEST_WRITER_FILENAME_PREFIX, str(PROCESS_GROUP_TEST_POLL_S)],
            start_new_session=True, stdout=subprocess.PIPE, text=True)
        descendant_pid = int(leader.stdout.readline())
        try:
            assert os.getpgid(leader.pid) == leader.pid
            assert os.getpgid(descendant_pid) == leader.pid
            if os.environ.get(PROCESS_GROUP_TEST_MUTATION_ENV) == PROCESS_GROUP_TEST_WORKER_ONLY_MUTATION:
                os.kill(leader.pid, signal.SIGKILL)
                leader.wait(timeout=PROCESS_GROUP_TEST_KILL_GRACE_S)
                reaped = True
            else:
                reaped = process_group_reaper.reap_process_group_bounded(
                    leader.pid, terminate_grace_s=PROCESS_GROUP_TEST_TERM_GRACE_S,
                    force_kill_grace_s=PROCESS_GROUP_TEST_KILL_GRACE_S,
                    poll_interval_s=PROCESS_GROUP_TEST_POLL_S,
                    warning_context=PROCESS_GROUP_TEST_WARNING_CONTEXT,
                    emit_warning=discard_process_group_reap_warning)
            assert reaped is True
            try:
                os.killpg(leader.pid, process_group_reaper.PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
            except ProcessLookupError as error:
                assert error.errno == errno.ESRCH
            else:
                raise AssertionError("cancel returned with a live process-group descendant")
            shutil.rmtree(repository)
            assert not repository.exists()
        finally:
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            leader.wait(timeout=PROCESS_GROUP_TEST_KILL_GRACE_S)


def assert_cancel_group_timeout_warns_and_returns():
    warnings = []
    monotonic_values = iter((0.0, PROCESS_GROUP_TEST_TERM_GRACE_S,
                             PROCESS_GROUP_TEST_TERM_GRACE_S,
                             PROCESS_GROUP_TEST_TERM_GRACE_S + PROCESS_GROUP_TEST_KILL_GRACE_S))
    with patch.object(process_group_reaper.os, "killpg", return_value=None), \
            patch.object(process_group_reaper.time, "monotonic", side_effect=monotonic_values):
        reaped = process_group_reaper.reap_process_group_bounded(
            os.getpid(), terminate_grace_s=PROCESS_GROUP_TEST_TERM_GRACE_S,
            force_kill_grace_s=PROCESS_GROUP_TEST_KILL_GRACE_S,
            poll_interval_s=PROCESS_GROUP_TEST_POLL_S,
            warning_context=PROCESS_GROUP_TEST_WARNING_CONTEXT,
            emit_warning=warnings.append)
    assert reaped is False
    assert len(warnings) == 1 and "still alive" in warnings[0] and "may still be writing" in warnings[0]


if __name__ == "__main__":
    only_timeout_guard = os.environ.get(PROCESS_GROUP_TEST_ONLY_TIMEOUT_ENV) == PROCESS_GROUP_TEST_ENABLED_VALUE
    if not only_timeout_guard:
        if host_permits_foreign_session_killpg():
            assert_cancel_group_guard_reaps_late_descendant()
        else:
            print(PROCESS_GROUP_TEST_EPERM_SKIP_REASON)
    assert_cancel_group_timeout_warns_and_returns()
    print("delegate cancel process-group guard: bounded timeout warning passed" if only_timeout_guard else
          "delegate cancel process-group guards: bounded timeout warning passed; real reap passed or skipped loudly")
