"""Offline lifecycle and lane isolation regressions; every process/runner is fixtured."""
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
from spendguard import codex_exec, gate, lane_registry, lanes, reliability

ISOLATION_FAILURE_REASON = "offline runner unavailable"
ISOLATION_STOP_REASON = "offline deliberate stop"
ISOLATION_TEST_TIMEOUT = codex_exec.CODEX_MCP_EXIT_WAIT_S


class RootExitAndLaneProbeIsolationTests(unittest.TestCase):
    def exercise_root_capture_fixture(self, root):
        child = MagicMock()
        child.__enter__.return_value = child
        child.pid = root.pid
        child.returncode = 0
        child.communicate.return_value = ("", "")
        child.poll.return_value = child.returncode
        with patch.object(codex_exec, "_codex_stdio_reap_policy", return_value=([], set())), \
                patch.object(codex_exec.subprocess, "Popen", return_value=child), \
                patch.object(psutil, "Process", return_value=root), \
                patch.object(codex_exec, "_reap_captured_codex_stdio"), \
                patch.object(codex_exec.threading, "Thread") as tracker, \
                patch.object(logging.getLogger(codex_exec.__name__), "handle") as logs:
            tracker.side_effect = lambda **kw: SimpleNamespace(start=lambda: kw["target"](*kw["args"]), join=lambda: None)
            result = codex_exec._run_cold_codex_with_reaper(
                [], timeout=ISOLATION_TEST_TIMEOUT, env={}, cwd=None)
        self.assertEqual(result.returncode, child.returncode)
        return [call.args[0] for call in logs.call_args_list]

    def test_codex_capture_exited_root_has_no_warning_traceback(self):
        for failure_type in (psutil.NoSuchProcess, psutil.AccessDenied):
            with self.subTest(failure_type=failure_type):
                root = MagicMock(pid=os.getpid())
                root.is_running.return_value = False
                root.children.side_effect = failure_type(root.pid)
                records = self.exercise_root_capture_fixture(root)
                self.assertFalse([record for record in records if record.levelno >= logging.WARNING or record.exc_info])

    def test_codex_capture_live_child_inspection_error_has_traceback(self):
        for failure_type in (RuntimeError, psutil.AccessDenied):
            with self.subTest(failure_type=failure_type):
                root = MagicMock(pid=os.getpid())
                descendant = MagicMock(pid=root.pid)
                descendant.is_running.return_value = True
                descendant.create_time.side_effect = failure_type(ISOLATION_FAILURE_REASON)
                root.children.side_effect = [[descendant], psutil.NoSuchProcess(root.pid)]
                records = self.exercise_root_capture_fixture(root)
                self.assertTrue([record for record in records if record.levelno >= logging.ERROR and record.exc_info])

    def exercise_lane_isolation_fixture(self, failure):
        failing_lane, healthy_lane, *_ = lane_registry.all_lanes()
        failing_runner = MagicMock(side_effect=failure)
        healthy_runner = MagicMock(return_value={"text": "ok"})
        modules = {failing_lane: SimpleNamespace(run_prompt=failing_runner),
                   healthy_lane: SimpleNamespace(run_prompt=healthy_runner)}
        status = {"lanes": [{"lane": name, "enabled": True} for name in modules]}
        with patch.object(lanes, "_lane_mods", return_value=modules), \
                patch.object(lanes, "lanes_status", return_value=status), \
                patch.object(lanes, "_record_probe"), \
                patch.object(reliability, "plan", return_value={"lanes": list(modules), "metered": []}):
            if isinstance(failure, gate.deliberate_stop_types()):
                for operation in (lanes.probe, lambda: reliability.sweep(run=True)):
                    with self.assertRaises(type(failure)):
                        operation()
                healthy_runner.assert_not_called()
                return
            rows = {row["lane"]: row for row in lanes.probe()}
            self.assertFalse(rows[failing_lane]["ok"])
            self.assertIn(str(failure), rows[failing_lane]["error"])
            self.assertTrue(rows[healthy_lane]["ok"])
            matrix = reliability.sweep(run=True)["lanes"]
            self.assertFalse(matrix[failing_lane]["reachable"])
            self.assertIn(str(failure), matrix[failing_lane]["reason"])
            self.assertTrue(matrix[healthy_lane]["reachable"])

    def test_probe_and_sweep_preserve_named_lane_failure_isolation(self):
        for failure_type in (FileNotFoundError, RuntimeError):
            with self.subTest(failure_type=failure_type):
                self.exercise_lane_isolation_fixture(failure_type(ISOLATION_FAILURE_REASON))

    def test_probe_and_sweep_propagate_deliberate_lane_stop(self):
        failure_type = gate.deliberate_stop_types()[0]
        self.exercise_lane_isolation_fixture(failure_type(ISOLATION_STOP_REASON))


if __name__ == "__main__":
    unittest.main()
