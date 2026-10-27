"""Offline guards for exception diagnostics without changing health outcomes."""
import copy
import contextlib
import logging
import traceback
import unittest
from unittest.mock import Mock, patch

from spendguard import adapters, gate, mcp_server, reliability

HEALTH_EXCEPTION_DETAIL = "health diagnostic full message " * 32 + "distinctive diagnostic tail"
HEALTH_RESOURCE_REASON = "resource unavailable"
HEALTH_TEST_SWEEP = {"lanes": {"test-lane": {"reachable": False, "reason": HEALTH_RESOURCE_REASON}},
                     "metered": {"test-provider": {"reachable": True}}}
HEALTH_TEST_ACTION = {"resource": "test-lane", "kind": "lane", "issue": HEALTH_RESOURCE_REASON,
                      "fix": "repair resource", "command": ""}


def _raise_health_diagnostic_origin(*args, **kwargs):
    raise RuntimeError(HEALTH_EXCEPTION_DETAIL)


class HealthExceptionDiagnosticGuard(unittest.TestCase):
    def assert_health_exception_evidence(self, diagnostic, captured):
        self.assertIn(HEALTH_EXCEPTION_DETAIL, diagnostic["exception"])
        self.assertIn(HEALTH_EXCEPTION_DETAIL, diagnostic["traceback"])
        self.assertIn(_raise_health_diagnostic_origin.__name__, diagnostic["traceback"])
        rendered = "\n".join(captured.output)
        self.assertIn(HEALTH_EXCEPTION_DETAIL, rendered)
        self.assertIn(_raise_health_diagnostic_origin.__name__, rendered)

    def test_health_sweep_exception_keeps_complete_diagnostic(self):
        with patch.object(reliability, "sweep", side_effect=_raise_health_diagnostic_origin):
            with self.assertLogs(level=logging.ERROR) as captured:
                result = mcp_server._tool_health({})
        self.assert_health_exception_evidence(result["diagnostic"], captured)
        self.assertIn(HEALTH_EXCEPTION_DETAIL, result["error"])
        self.assertEqual(result["lanes"], {})
        self.assertEqual(result["metered"], {})
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["summary"], {"lanes_up": 0, "lanes_total": 0,
                         "metered_up": 0, "metered_total": 0, "down": []})

    def test_health_post_sweep_failure_keeps_success_values(self):
        for failing_step in ("remediate", "_persist_health"):
            with self.subTest(failing_step=failing_step):
                with patch.object(reliability, "sweep", return_value=copy.deepcopy(HEALTH_TEST_SWEEP)), \
                     patch.object(reliability, "remediate", return_value=[copy.deepcopy(HEALTH_TEST_ACTION)]) as remediate, \
                     patch.object(reliability, "_persist_health") as persist:
                    baseline = mcp_server._tool_health({})
                    if failing_step == "remediate":
                        remediate.side_effect = _raise_health_diagnostic_origin
                        baseline["actions"] = []
                    else:
                        persist.side_effect = _raise_health_diagnostic_origin
                    with self.assertLogs(level=logging.ERROR) as captured:
                        result = mcp_server._tool_health({})
                self.assert_health_exception_evidence(result.pop("diagnostic"), captured)
                self.assertEqual(result, baseline)

    def test_resource_remediation_failure_keeps_exception_and_resource(self):
        database = Mock()
        database.execute.return_value.fetchone.return_value = None
        with patch.object(reliability, "_remediation_db", return_value=database), \
             patch.object(reliability.config, "ledger_op", side_effect=lambda *args: contextlib.nullcontext(database)), \
             patch.object(adapters, "call", side_effect=_raise_health_diagnostic_origin), \
             patch.object(gate, "is_deliberate_stop", return_value=False):
            with self.assertLogs(level=logging.ERROR) as captured:
                result = reliability._classify_remediation("lane", "test-lane", HEALTH_RESOURCE_REASON,
                                                          model="test-model")
        self.assert_health_exception_evidence(result.pop("diagnostic"), captured)
        self.assertEqual(result, {"issue": HEALTH_RESOURCE_REASON,
                         "fix": "inspect the lane's auth/quota/model config", "command": "", "cached": False})

    def test_resource_remediation_deliberate_stop_keeps_original_traceback(self):
        database = Mock()
        database.execute.return_value.fetchone.return_value = None
        with patch.object(reliability, "_remediation_db", return_value=database), \
             patch.object(reliability.config, "ledger_op", side_effect=lambda *args: contextlib.nullcontext(database)), \
             patch.object(adapters, "call", side_effect=_raise_health_diagnostic_origin), \
             patch.object(gate, "is_deliberate_stop", return_value=True):
            try:
                reliability._classify_remediation("lane", "test-lane", HEALTH_RESOURCE_REASON, model="test-model")
            except RuntimeError as error:
                frames = traceback.extract_tb(error.__traceback__)
                self.assertEqual(str(error), HEALTH_EXCEPTION_DETAIL)
                self.assertEqual(sum(frame.name == "_classify_remediation" for frame in frames), 1)
                self.assertEqual(frames[-1].name, _raise_health_diagnostic_origin.__name__)
            else:
                self.fail("deliberate stop did not propagate")

    def test_resource_remediation_success_values_unchanged(self):
        database = Mock()
        database.execute.return_value.fetchone.return_value = None
        expected = {"issue": "resource issue", "fix": "resource fix", "command": "resource command"}
        with patch.object(reliability, "_remediation_db", return_value=database), \
             patch.object(reliability.config, "ledger_op", side_effect=lambda *args: contextlib.nullcontext(database)), \
             patch.object(adapters, "call", return_value={}), \
             patch.object(adapters, "structured_reply", return_value=expected):
            result = reliability._classify_remediation("lane", "test-lane", HEALTH_RESOURCE_REASON, model="test-model")
        self.assertEqual(result, {**expected, "cached": False})

    def test_health_actions_retain_resource_remediation_diagnostic(self):
        database = Mock()
        database.execute.return_value.fetchone.return_value = None
        with patch.object(reliability, "sweep", return_value=copy.deepcopy(HEALTH_TEST_SWEEP)), \
             patch.object(reliability, "_persist_health"), \
             patch.object(reliability, "_remediation_db", return_value=database), \
             patch.object(reliability.config, "ledger_op", side_effect=lambda *args: contextlib.nullcontext(database)), \
             patch.object(reliability.config, "advisor_model", return_value="test-model"), \
             patch.object(adapters, "call", side_effect=_raise_health_diagnostic_origin), \
             patch.object(gate, "is_deliberate_stop", return_value=False):
            with self.assertLogs(level=logging.ERROR) as captured:
                result = mcp_server._tool_health({})
        action = result["actions"][0]
        self.assert_health_exception_evidence(action.pop("diagnostic"), captured)
        self.assertEqual(action, {**HEALTH_TEST_ACTION, "fix": "inspect the lane's auth/quota/model config"})


if __name__ == "__main__":
    unittest.main()
