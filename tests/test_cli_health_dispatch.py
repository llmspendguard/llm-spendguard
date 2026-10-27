"""Offline regression: the CLI exposes the exact MCP health result and callable."""
import contextlib
import io
import json
import os
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-cli-health-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import cli, mcp_server  # noqa: E402

HEALTH_TEST_TIMEOUT_SECONDS = 7
HEALTH_TEST_RESULT = {
    "lanes": {"test-subscription": {"reachable": True, "latency": 0.25, "reason": None}},
    "metered": {"test-provider": {"reachable": False, "latency": 0.5, "reason": "offline fixture"}},
    "summary": {"lanes_up": 1, "lanes_total": 1, "metered_up": 0, "metered_total": 1,
                "down": ["metered:test-provider"]},
    "actions": [],
    "ledger": {"status": "cached", "line": "offline ledger fixture", "age_s": 12},
    "note": "offline fixture",
}


def exercise_cli_health_dispatch():
    health_cli_stdout = io.StringIO()
    health_cli_stderr = io.StringIO()
    with patch.object(mcp_server, "_tool_health", return_value=HEALTH_TEST_RESULT) as shared_health_call:
        with contextlib.redirect_stdout(health_cli_stdout), contextlib.redirect_stderr(health_cli_stderr):
            health_cli_status = cli.main(["health", "--run", "--timeout_s", str(HEALTH_TEST_TIMEOUT_SECONDS)])
        assert health_cli_status == 0, (health_cli_status, health_cli_stderr.getvalue())
        shared_health_call.assert_called_once()
        health_cli_arguments = shared_health_call.call_args.args[0]
        assert health_cli_arguments["run"] is True
        assert health_cli_arguments["timeout_s"] == HEALTH_TEST_TIMEOUT_SECONDS
        health_cli_text = health_cli_stdout.getvalue()
        for health_fixture_name in (*HEALTH_TEST_RESULT["lanes"], *HEALTH_TEST_RESULT["metered"]):
            assert health_fixture_name in health_cli_text, health_cli_text
        assert HEALTH_TEST_RESULT["metered"]["test-provider"]["reason"] in health_cli_text
        assert HEALTH_TEST_RESULT["ledger"]["line"] in health_cli_text
        assert str(HEALTH_TEST_RESULT["ledger"]["age_s"]) in health_cli_text

    health_cli_stdout = io.StringIO()
    with patch.object(mcp_server, "_tool_health", return_value=HEALTH_TEST_RESULT) as default_health_call:
        with contextlib.redirect_stdout(health_cli_stdout):
            health_cli_status = cli.main(["health", "--json"])
    assert health_cli_status == 0
    assert json.loads(health_cli_stdout.getvalue()) == HEALTH_TEST_RESULT
    assert default_health_call.call_args.args[0]["run"] == mcp_server.HEALTH_DEFAULT_RUN
    assert default_health_call.call_args.args[0]["timeout_s"] == mcp_server.HEALTH_DEFAULT_TIMEOUT_S

    with patch.object(mcp_server, "_tool_health", return_value=HEALTH_TEST_RESULT) as estimate_health_call:
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main(["health", "--no-run"]) == 0
    assert estimate_health_call.call_args.args[0]["run"] is False

    health_error_result = {**HEALTH_TEST_RESULT, "error": "offline health call failed"}
    with patch.object(mcp_server, "_tool_health", return_value=health_error_result):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["health", "--json"]) != 0
    with patch.object(mcp_server, "_tool_health", side_effect=RuntimeError(health_error_result["error"])):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["health"]) != 0
    print("OK test_cli_health_dispatch: shared MCP callable, lane rendering, down lane exits zero, raw JSON")


def exercise_mcp_health_cached_ledger():
    from spendguard import ledger_sync, reliability

    health_sweep_fixture = {"lanes": {}, "metered": {}}
    ledger_fixture = HEALTH_TEST_RESULT["ledger"]
    with patch.object(reliability, "sweep", return_value=health_sweep_fixture) as offline_health_sweep:
        with patch.object(ledger_sync, "cached_leak_line", return_value=(ledger_fixture["line"], ledger_fixture["age_s"])):
            assert mcp_server._tool_health({"run": False})["ledger"] == ledger_fixture
        offline_health_sweep.assert_called_once_with(run=False, timeout_s=mcp_server.HEALTH_DEFAULT_TIMEOUT_S)
        with patch.object(ledger_sync, "cached_leak_line", return_value=None):
            missing_ledger_health = mcp_server._tool_health({"run": False})["ledger"]
        assert missing_ledger_health["status"] == "unknown"
        assert missing_ledger_health["age_s"] is None
        with patch.object(ledger_sync, "cached_leak_line", side_effect=RuntimeError(ledger_fixture["line"])):
            failed_ledger_health = mcp_server._tool_health({"run": False})["ledger"]
        assert failed_ledger_health["status"] == "unknown"
        assert failed_ledger_health["age_s"] is None
        assert failed_ledger_health["error"]
    print("OK test_cli_health_dispatch: shared cached/missing/failed ledger status")


exercise_cli_health_dispatch()
exercise_mcp_health_cached_ledger()
