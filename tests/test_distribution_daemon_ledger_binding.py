"""The daemon's own ledger identity must govern the capstone descriptor proof."""
import contextlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import distribution, distribution_daemon, mcp_fd_lifecycle, mcp_server

LEDGER_BINDING_PID = 812
LEDGER_BINDING_VERSION = "ledger-binding-current"
LEDGER_BINDING_SHA = "ledger-binding-green"
LEDGER_BINDING_ENDPOINT = "http://ledger-binding.invalid/mcp"
LEDGER_BINDING_FD = 11
LEDGER_BINDING_COUNT = 4
LEDGER_BINDING_LIMIT = 100
LEDGER_BINDING_FRACTION = 0.75
LEDGER_BINDING_POLL = 1.0


def exercise_distribution_foreign_home_ledger_binding():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checker_ledger = root / "checker" / "spend.db"
        daemon_ledger = root / "daemon" / "spend.db"
        target = {"version": LEDGER_BINDING_VERSION, "sha": LEDGER_BINDING_SHA}
        status = {"pid": LEDGER_BINDING_PID, "imported_version": LEDGER_BINDING_VERSION,
                  "metadata_version": LEDGER_BINDING_VERSION, "ledger_path": str(daemon_ledger),
                  "served": {"sha": LEDGER_BINDING_SHA, "dirty": False}}
        with patch.object(distribution.release, "release_status", return_value={}), patch.object(
                mcp_fd_lifecycle.config, "db_path", return_value=str(daemon_ledger)):
            assert mcp_server._tool_version({})["ledger_path"] == str(daemon_ledger.resolve())
        consumer = {"pid": LEDGER_BINDING_PID,
                    "supervision_ref": distribution_daemon.MCP_HTTP_LAUNCHD_LABEL}
        files = [SimpleNamespace(path=str(daemon_ledger), fd=LEDGER_BINDING_FD)]
        process = SimpleNamespace(open_files=lambda: files, num_fds=lambda: LEDGER_BINDING_COUNT)

        def answer_ledger_binding_rpc(endpoint, method, params, timeout):
            assert endpoint == LEDGER_BINDING_ENDPOINT
            if method == "initialize":
                return {"serverInfo": {"version": LEDGER_BINDING_VERSION}}
            return {"structuredContent": status}

        with contextlib.ExitStack() as stack:
            for owner, name, value in (
                (distribution, "resolve_distribution_target", target),
                (distribution.setup, "discover_distribution_interpreters", {"interpreters": [], "gaps": []}),
                (distribution.consumer_audit, "audit_consumer_workspaces", {"consumers": [], "pruned_at_depth": []}),
                (distribution, "attest_distribution_host", distribution.distribution_surface_row("host", "host", True, True)),
                (distribution, "attest_distribution_api_consumers", []),
                (distribution.fleet, "running_consumers", [consumer]),
                (distribution.fleet, "_classify_fleet_pid_identity", distribution.fleet._FLEET_PID_VERIFIED_LIVE),
                (mcp_fd_lifecycle.psutil, "Process", process),
                (mcp_fd_lifecycle.config, "db_path", str(checker_ledger)),
                (mcp_fd_lifecycle, "mcp_descriptor_policy", (LEDGER_BINDING_LIMIT, LEDGER_BINDING_FRACTION, LEDGER_BINDING_POLL)),
                (distribution_daemon, "distribution_daemon_settings", {"mcp_endpoint": LEDGER_BINDING_ENDPOINT, "probe_timeout_s": LEDGER_BINDING_POLL}),
            ):
                stack.enter_context(patch.object(owner, name, return_value=value))
            stack.enter_context(patch.object(distribution_daemon, "request_distribution_mcp_rpc", side_effect=answer_ledger_binding_rpc))

            def assert_foreign_home_ledger_is_red():
                result = distribution.distribute_fleet(check=True, settings={})
                named = f"mcp-process:{LEDGER_BINDING_PID}"
                assert result["exit_code"] != 0 and named in result["summary"]["stragglers"], result
                row = next(row for row in result["surfaces"] if row["surface"] == named)
                assert row["fd_ok"] is False and row["straggler_reason"], row
                return result

            assert_foreign_home_ledger_is_red()
            original = mcp_fd_lifecycle.attest_mcp_process_descriptors

            def restore_checker_default_ledger(pid, ledger_path=None):
                return original(pid, ledger_path=str(checker_ledger))

            with patch.object(mcp_fd_lifecycle, "attest_mcp_process_descriptors", side_effect=restore_checker_default_ledger):
                try:
                    assert_foreign_home_ledger_is_red()
                except AssertionError:
                    pass
                else:
                    raise AssertionError("checker-home regression survived the known-leak assertion")
            files.clear()
            result = distribution.distribute_fleet(check=True, settings={})
            assert result["exit_code"] == 0 and result["summary"]["stragglers"] == [], result
            del status["ledger_path"]
            assert_foreign_home_ledger_is_red()
    print("OK foreign-home ledger leak named and nonzero; checker-home mutant red; clean fixture green; missing binding closed")


exercise_distribution_foreign_home_ledger_binding()
