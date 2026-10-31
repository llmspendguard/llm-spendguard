"""Offline regression proof for the shared gate child environment and CLI barrier."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import spendguard

spendguard.require()

HERMETICITY_REPO_ROOT = Path(__file__).resolve().parents[1]
HERMETICITY_CLOCK_ORIGIN = 0
HERMETICITY_SUCCESS_RETURN_CODE = 0
HERMETICITY_SQLITE_FILENAME = "hermeticity.sqlite3"
HERMETICITY_FROZEN_HOME_SQLITE_FILENAME = "frozen-home-hermeticity.sqlite3"
HERMETICITY_SQLITE_READ_ONLY_URI_QUERY = "mode=ro"
HERMETICITY_SQLITE_PROOF_DDL = "CREATE TABLE isolation_proof (proof_value INTEGER)"
HERMETICITY_COVERAGE_PARALLEL_SUFFIX = "host.pid.random"
sys.path.insert(0, str(HERMETICITY_REPO_ROOT))
from scripts.test import chunked_suite  # noqa: E402
from spendguard import config, conv  # noqa: E402
import test_runner as hermeticity_pytest_runner  # noqa: E402


def verify_gate_hermeticity_isolation_contract():
    with tempfile.TemporaryDirectory(dir=chunked_suite.GATE_SHORT_TMPDIR_BASE) as scratch_home, \
            tempfile.TemporaryDirectory(dir=chunked_suite.GATE_SHORT_TMPDIR_BASE) as scratch_tmpdir, \
            tempfile.TemporaryDirectory(dir=chunked_suite.GATE_SHORT_TMPDIR_BASE) as external_tool_home:
        expected_home = str(Path(scratch_home).resolve())
        expected_tmpdir = str(Path(scratch_tmpdir).resolve())
        environment = chunked_suite._child_env(
            scratch_home, scratch_tmpdir, hermeticity_pytest_runner.MIN_RUNNER_SCALE
        )
        assert hermeticity_pytest_runner._child_env is chunked_suite._child_env
        assert hermeticity_pytest_runner._child_env(
            scratch_home, scratch_tmpdir, hermeticity_pytest_runner.MIN_RUNNER_SCALE
        ) == environment
        for home_key in chunked_suite.GATE_ISOLATED_HOME_ENV_NAMES:
            assert environment[home_key] == expected_home
        assert environment[chunked_suite.GATE_ISOLATED_TMPDIR_ENV_NAME] == expected_tmpdir
        assert environment[chunked_suite.GATE_TEST_ISOLATED_ENV] == chunked_suite.GATE_TEST_ENABLED
        assert environment[config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY] == config.CLI_TEST_OFFLINE_ENABLED_VALUE
        with patch.dict(os.environ, environment):
            with sqlite3.connect(Path(scratch_home) / HERMETICITY_SQLITE_FILENAME) as isolated_connection:
                isolated_connection.execute(HERMETICITY_SQLITE_PROOF_DDL)
            with tempfile.TemporaryDirectory() as redirected_home, patch.dict(
                    os.environ, {
                        config.LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY: redirected_home,
                        config.LEDGER_HOME_ENVIRONMENT_KEY: redirected_home,
                    }):
                os.environ.pop(chunked_suite.GATE_ISOLATED_TMPDIR_ENV_NAME, None)
                frozen_home_database = config.HOME / HERMETICITY_FROZEN_HOME_SQLITE_FILENAME
                with sqlite3.connect(frozen_home_database) as frozen_home_connection:
                    frozen_home_connection.execute(HERMETICITY_SQLITE_PROOF_DDL)
            outside_uri = (
                f"{config.SQLITE_FILE_URI_SCHEME}:{Path(__file__).resolve()}?"
                f"{HERMETICITY_SQLITE_READ_ONLY_URI_QUERY}"
            )
            try:
                sqlite3.connect(outside_uri, uri=True)
            except config.IsolatedSQLitePathError as guard_error:
                print(f"[OK] SQLite confinement guard fired: {guard_error}")
            else:
                raise AssertionError("isolated SQLite connection escaped scratch roots")
            coverage_data_path = Path(external_tool_home) / (
                f"{config.COVERAGE_DEFAULT_DATA_FILE_NAME}.{HERMETICITY_COVERAGE_PARALLEL_SUFFIX}"
            )
            with sqlite3.connect(coverage_data_path) as coverage_connection:
                coverage_connection.execute(HERMETICITY_SQLITE_PROOF_DDL)
            assert coverage_data_path.is_file()
        with patch.object(hermeticity_pytest_runner.tempfile, "mkdtemp",
                          side_effect=[scratch_home, scratch_tmpdir]), \
                patch.object(hermeticity_pytest_runner.time, "monotonic", return_value=HERMETICITY_CLOCK_ORIGIN), \
                patch.object(hermeticity_pytest_runner.subprocess, "run", return_value=SimpleNamespace(
                    returncode=HERMETICITY_SUCCESS_RETURN_CODE, stdout="", stderr="")) as runner_child:
            hermeticity_pytest_runner.test_script(__file__, hermeticity_pytest_runner.MIN_RUNNER_SCALE)
        # Coverage may add its startup key; every shared isolation key must survive unchanged.
        # The pytest runner intentionally replaces the standalone runner's locally measured
        # scale with the controller fixture value passed to test_script.
        expected_runner_environment = dict(environment)
        expected_runner_environment[chunked_suite.RUNNER_BUDGET_SCALE_ENV] = str(
            hermeticity_pytest_runner.MIN_RUNNER_SCALE)
        assert expected_runner_environment.items() <= runner_child.call_args.kwargs["env"].items()
        for broken_key in (*chunked_suite.GATE_ISOLATED_HOME_ENV_NAMES,
                           chunked_suite.GATE_ISOLATED_TMPDIR_ENV_NAME,
                           chunked_suite.GATE_TEST_ISOLATED_ENV, config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY):
            broken_environment = dict(environment)
            broken_environment.pop(broken_key)
            try:
                chunked_suite.validate_gate_child_environment(broken_environment, scratch_home, scratch_tmpdir)
            except chunked_suite.GateTestEnvironmentError:
                pass
            else:
                raise AssertionError(f"missing {broken_key} accepted")
        escaped_environment = dict(environment)
        escaped_environment["HOME"] = str(Path(scratch_home).parent)
        try:
            chunked_suite.validate_gate_child_environment(escaped_environment, scratch_home, scratch_tmpdir)
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
