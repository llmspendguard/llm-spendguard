"""Offline lifecycle guards: session spawn, descendant signalling, races and fallback."""
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from spendguard import warm_stdio_daemon as warm

_WARM_TEST_PID = os.getpid()
_WARM_TEST_GRACE_S = 0
_WARM_TREE_READY_TIMEOUT_S = warm._WARM_REAP_TIMEOUT_S
_WARM_TREE_PROGRAM = r'''
import os
import signal
import subprocess
import sys
child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
def reap_descendant(signum, frame):
    child.wait()
    sys.exit(0)
signal.signal(signal.SIGTERM, reap_descendant)
print(child.pid, flush=True)
signal.pause()
'''


class WarmDaemonGroupTeardownGuard(unittest.TestCase):
    def test_warm_spawn_starts_owned_session(self):
        daemon = warm._WarmStdioJsonRpcDaemon()
        daemon._spawn_cmd = lambda: [sys.executable]
        daemon._handshake = lambda process: True
        process = Mock(pid=_WARM_TEST_PID)
        with patch.object(warm.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(warm.fcntl, "fcntl", return_value=0), \
                patch.object(warm.threading.Thread, "start"), \
                patch.object(warm.config, "lane_exec_env", return_value={}):
            self.assertIs(daemon._spawn(), process)
        supported = hasattr(os, "killpg") and hasattr(os, "getpgid")
        self.assertEqual(spawn.call_args.kwargs["start_new_session"], supported)
        if supported:
            self.assertEqual(process._spendguard_process_group, process.pid)

    @unittest.skipUnless(hasattr(os, "killpg") and hasattr(os, "getpgid"), "POSIX process groups required")
    def test_warm_group_escalates_after_leader_has_exited(self):
        process = Mock(pid=_WARM_TEST_PID, _spendguard_process_group=_WARM_TEST_PID)
        with patch.object(warm.os, "getpgid", side_effect=ProcessLookupError), \
                patch.object(warm.os, "killpg") as group_signal, \
                patch.object(warm, "_WARM_REAP_TIMEOUT_S", _WARM_TEST_GRACE_S):
            warm._WarmStdioJsonRpcDaemon._terminate(process)
        delivered = [entry.args for entry in group_signal.call_args_list
                     if entry.args[1] != warm._WARM_GROUP_PROBE_SIGNAL]
        self.assertEqual(delivered, [(_WARM_TEST_PID, signal.SIGTERM), (_WARM_TEST_PID, signal.SIGKILL)])
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(process.wait.call_count, 2)

    @unittest.skipUnless(hasattr(os, "killpg") and hasattr(os, "getpgid"), "POSIX process groups required")
    def test_warm_group_uses_resolved_child_pgid_and_records_failures(self):
        process = Mock(pid=_WARM_TEST_PID, _spendguard_process_group=_WARM_TEST_PID)
        warnings = []
        with patch.object(warm.os, "getpgid", return_value=process.pid) as resolve, \
                patch.object(warm.os, "killpg", side_effect=PermissionError("group denied")), \
                patch.object(warm.config, "warn_once", side_effect=warnings.append), \
                patch.object(warm, "_WARM_REAP_TIMEOUT_S", _WARM_TEST_GRACE_S):
            warm._WarmStdioJsonRpcDaemon._terminate(process)
        resolve.assert_called_once_with(process.pid)
        self.assertTrue(warnings)
        self.assertTrue(all("group denied" in warning for warning in warnings))

    def test_warm_spawn_setup_exception_reaps_session(self):
        daemon = warm._WarmStdioJsonRpcDaemon()
        daemon._spawn_cmd = lambda: [sys.executable]
        process = Mock(pid=_WARM_TEST_PID)
        with patch.object(warm.subprocess, "Popen", return_value=process), \
                patch.object(warm.config, "lane_exec_env", return_value={}), \
                patch.object(warm.fcntl, "fcntl", side_effect=OSError), \
                patch.object(daemon, "_terminate") as reap:
            with self.assertRaises(OSError):
                daemon._spawn()
        reap.assert_called_once_with(process)

    def test_warm_restart_reaps_dead_leader_before_replacement(self):
        daemon = warm._WarmStdioJsonRpcDaemon()
        previous = Mock()
        previous.poll.return_value = 0
        daemon._proc = previous
        replacement = Mock()
        order = []
        with patch.object(daemon, "_terminate", side_effect=lambda process: order.append(process)), \
                patch.object(daemon, "_spawn", side_effect=lambda: order.append(replacement) or replacement):
            self.assertIs(daemon.ensure_running(), replacement)
        self.assertEqual(order, [previous, replacement])

    def test_warm_no_group_api_preserves_direct_escalation(self):
        process = Mock(pid=_WARM_TEST_PID)
        process.wait.side_effect = [subprocess.TimeoutExpired(sys.executable, warm._WARM_REAP_TIMEOUT_S), None]
        with patch.object(warm.os, "killpg", create=True) as group_signal:
            with patch.object(warm.os, "getpgid", create=True):
                del warm.os.getpgid
                warm._WarmStdioJsonRpcDaemon._terminate(process)
            group_signal.assert_not_called()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual([entry.kwargs["timeout"] for entry in process.wait.call_args_list],
                         [warm._WARM_REAP_TIMEOUT_S, warm._WARM_REAP_TIMEOUT_S])

    @unittest.skipUnless(hasattr(os, "killpg") and hasattr(os, "getpgid"), "POSIX process groups required")
    def test_warm_real_child_and_grandchild_are_reaped(self):
        # The child explicitly wait()s its grandchild, so this asserts reaping, not only death/zombies.
        process = subprocess.Popen([sys.executable, "-c", _WARM_TREE_PROGRAM],
                                   stdout=subprocess.PIPE, text=True, start_new_session=True)
        process._spendguard_process_group = process.pid
        grandchild_pid = None
        try:
            import select
            self.assertTrue(select.select([process.stdout], [], [], _WARM_TREE_READY_TIMEOUT_S)[0])
            grandchild_pid = int(process.stdout.readline())
            daemon = warm._WarmStdioJsonRpcDaemon()
            daemon._proc = process
            daemon.shutdown()
            self.assertIsNotNone(process.returncode)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild_pid, warm._WARM_GROUP_PROBE_SIGNAL)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=warm._WARM_REAP_TIMEOUT_S)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
