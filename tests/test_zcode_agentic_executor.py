"""Offline contract for ZCode agentic selection, authentication, and key isolation."""

import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-zcode-test-"))
os.environ["SPENDGUARD_TEST_CLI_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import config  # noqa: E402
from spendguard.generate.execution import zcode_exec  # noqa: E402
from spendguard.store.configuration import lane_subprocess_env  # noqa: E402
from spendguard.store.reference_data import lane_registry  # noqa: E402


class ZCodeAgenticExecutorContractTests(unittest.TestCase):
    def test_zcode_plan_environment_scrubs_provider_keys(self):
        with tempfile.TemporaryDirectory(prefix="sg-zcode-bin-") as directory:
            executable = str(Path(directory) / "zcode")
            completed = subprocess.CompletedProcess(args=[], returncode=0,
                                                    stdout='{"response":"ok","usage":{}}', stderr="")
            captured = {}

            def capture_zcode_subprocess(command, **kwargs):
                captured.update(kwargs)
                return completed

            keys = {"ANTHROPIC_API_KEY": secrets.token_hex(),
                    "ZAI_CODING_API_KEY": secrets.token_hex()}
            with (patch.dict(os.environ, keys),
                  patch.object(lane_subprocess_env, "refuse_isolated_cli_launch"),
                  patch.object(zcode_exec, "_bin", return_value=executable),
                  patch.object(zcode_exec.subprocess, "run", side_effect=capture_zcode_subprocess),
                  patch.object(config, "lane_exec_env", wraps=config.lane_exec_env) as plan_environment):
                result = zcode_exec.run_prompt("offline fixture", agentic=True, cwd=directory)
            self.assertIsNone(result["error"])
            plan_environment.assert_called_once_with(executable)
            self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
            self.assertNotIn("ZAI_CODING_API_KEY", captured["env"])
            self.assertIs(captured["stdin"], subprocess.DEVNULL)

    def test_zcode_agentic_selection_preserves_zai_completion(self):
        spec = lane_registry.lane_spec("zai-coding")
        self.assertEqual(spec.get("agentic_exec") or spec["exec"], "zcode_exec")
        self.assertEqual(spec["exec"], "zai_exec")

    def test_agentic_lane_executors_resolve_from_the_canonical_home(self):
        # Anti-amnesia guard for the ModuleNotFoundError that shipped green: the string-only
        # selection test above never drove the importlib call the router makes, so an executor
        # declared in the registry but unimportable (e.g. a new `zcode_exec` not resolvable via
        # the legacy-alias path) passed the suite and only failed on the live host. This resolves
        # EVERY agentic lane's declared executor exactly as delegate_router does, and drives the
        # router's own readiness pass — the exact call site that raised
        # "No module named 'spendguard.zcode_exec'".
        import importlib

        from spendguard.manage.token_operations import delegate_router

        agentic_lanes = [name for name in lane_registry.all_lanes()
                         if lane_registry.lane_spec(name).get("agentic")]
        self.assertIn("zai-coding", agentic_lanes)
        for lane in agentic_lanes:
            spec = lane_registry.lane_spec(lane)
            module_name = spec.get("agentic_exec") or spec["exec"]
            module = importlib.import_module(f"spendguard.generate.execution.{module_name}")
            self.assertTrue(callable(getattr(module, "run_prompt", None)),
                            f"{lane} agentic executor {module_name} lacks run_prompt")
        # Drive the router's OWN import line (not a copy of it): if the router resolved executors by
        # the legacy-alias path again, a new/undeclared executor would raise ModuleNotFoundError here,
        # exactly as it did on the live host. Readiness never calls run_prompt; patching the offline
        # guard off lets codex/grok/etc. auth_status (file reads) run without a real CLI launch.
        with patch.object(lane_subprocess_env, "refuse_isolated_cli_launch"):
            ready = delegate_router.delegation_lanes_ready("agentic")
        self.assertIsInstance(ready.get("ready"), list)

    def test_zcode_registry_invariants_hold(self):
        spec = lane_registry.lane_spec("zai-coding")
        self.assertTrue(spec["agentic"])
        self.assertEqual(spec["agentic_exec"], "zcode_exec")
        self.assertEqual(len({row["provider"] for row in lane_registry.LANES}), len(lane_registry.LANES))
        self.assertEqual(len({row["lane"] for row in lane_registry.LANES}), len(lane_registry.LANES))

    def test_zcode_availability_and_auth_are_pure(self):
        with patch.object(zcode_exec, "_bin", return_value=None):
            self.assertFalse(zcode_exec.available())
            self.assertFalse(zcode_exec.auth_status()["authed"])
        with tempfile.NamedTemporaryFile(prefix="sg-zcode-auth-") as artifact:
            with (patch.object(zcode_exec, "_bin", return_value=artifact.name),
                  patch.object(zcode_exec, "ZCODE_CONFIG_PATH", Path(artifact.name))):
                self.assertTrue(zcode_exec.available())
                self.assertTrue(zcode_exec.auth_status()["authed"])


if __name__ == "__main__":
    unittest.main()
