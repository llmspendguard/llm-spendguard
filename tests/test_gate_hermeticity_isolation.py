"""Offline regression proof for the shared gate child environment and CLI barrier."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import spendguard

spendguard.require()

HERMETICITY_REPO_ROOT = Path(__file__).resolve().parents[1]
HERMETICITY_CLOCK_ORIGIN = 0
HERMETICITY_SUCCESS_RETURN_CODE = 0
sys.path.insert(0, str(HERMETICITY_REPO_ROOT))
from scripts.test import chunked_suite  # noqa: E402
from spendguard import config, conv  # noqa: E402
import test_runner as hermeticity_pytest_runner  # noqa: E402


def verify_gate_hermeticity_isolation_contract():
    with tempfile.TemporaryDirectory() as scratch:
        expected_home = str(Path(scratch).resolve())
        environment = chunked_suite._child_env(scratch)
        assert hermeticity_pytest_runner._child_env is chunked_suite._child_env
        assert hermeticity_pytest_runner._child_env(scratch) == environment
        for home_key in chunked_suite.GATE_ISOLATED_HOME_ENV_NAMES:
            assert environment[home_key] == expected_home
        assert environment[chunked_suite.GATE_TEST_ISOLATED_ENV] == chunked_suite.GATE_TEST_ENABLED
        assert environment[config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY] == config.CLI_TEST_OFFLINE_ENABLED_VALUE
        with patch.object(hermeticity_pytest_runner.tempfile, "mkdtemp", return_value=scratch), \
                patch.object(hermeticity_pytest_runner.time, "monotonic", return_value=HERMETICITY_CLOCK_ORIGIN), \
                patch.object(hermeticity_pytest_runner.subprocess, "run", return_value=SimpleNamespace(
                    returncode=HERMETICITY_SUCCESS_RETURN_CODE, stdout="", stderr="")) as runner_child:
            hermeticity_pytest_runner.test_script(__file__, hermeticity_pytest_runner.MIN_RUNNER_SCALE)
        # Coverage may add its startup key; every shared isolation key must survive unchanged.
        assert environment.items() <= runner_child.call_args.kwargs["env"].items()
        for broken_key in (*chunked_suite.GATE_ISOLATED_HOME_ENV_NAMES,
                           chunked_suite.GATE_TEST_ISOLATED_ENV, config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY):
            broken_environment = dict(environment)
            broken_environment.pop(broken_key)
            try:
                chunked_suite.validate_gate_child_environment(broken_environment, scratch)
            except chunked_suite.GateTestEnvironmentError:
                pass
            else:
                raise AssertionError(f"missing {broken_key} accepted")
        escaped_environment = dict(environment)
        escaped_environment["HOME"] = str(Path(scratch).parent)
        try:
            chunked_suite.validate_gate_child_environment(escaped_environment, scratch)
        except chunked_suite.GateTestEnvironmentError:
            pass
        else:
            raise AssertionError("escaped HOME accepted")
    with patch.dict(os.environ, {config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY: config.CLI_TEST_OFFLINE_ENABLED_VALUE}):
        for cli_attempt in (config.refuse_isolated_cli_launch, lambda: config.lane_exec_env(None)):
            try:
                cli_attempt()
            except config.IsolatedCliLaunchError:
                pass
            else:
                raise AssertionError("isolated CLI launch accepted")
    with patch.dict(os.environ):
        os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
        config.refuse_isolated_cli_launch()
        assert config.lane_exec_env(None) == config.lane_plan_env()
    assert Path(conv._DEFAULT_TDIR).is_relative_to(Path(os.environ["HOME"]))
    print("[OK] shared gate isolation, transcript HOME root, and CLI barrier")


if __name__ == "__main__":
    verify_gate_hermeticity_isolation_contract()
