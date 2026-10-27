"""Offline, individual removal mutants for the remaining capstone proof seams."""
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import distribution, distribution_daemon, fleet, mcp_fd_lifecycle, setup
from test_distribution_guard_removal import (
    GUARD_REMOVAL_TIMEOUT,
    GUARD_REMOVAL_VERSION, compile_distribution_guard_removal,
    distribution_daemon_guard_fixture, require_distribution_guard_mutant_killed,
)
from test_distribution_inventory_fail_closed import (
    assert_distribution_inventory_named_red, distribution_inventory_fixture,
)

REMAINING_GUARD_BUDGET = 1
REMAINING_GUARD_LIMIT = 100
REMAINING_GUARD_FRACTION = 0.75
REMAINING_GUARD_FAILURE = "fixture inventory denied"


def assert_remaining_capstone_gap(surface, settings):
    result = distribution.distribute_fleet(check=True, settings=settings)
    assert_distribution_inventory_named_red(result, str(surface))
    return result


def test_distribution_aggregation_unhealthy_guard_removal():
    with distribution_inventory_fixture() as (_, _, _, settings), patch.object(
            distribution, "attest_distribution_host", return_value=distribution.distribution_surface_row(
                "host", "host", True, False)):
        mutant = compile_distribution_guard_removal(distribution.distribute_fleet, (
            ('int(bool(stragglers) or not rows)', 'int(not rows)'),))
        require_distribution_guard_mutant_killed(
            lambda: assert_remaining_capstone_gap("host", settings), distribution, "distribute_fleet", mutant)


def test_distribution_aggregation_denied_inventory_guard_removal():
    with distribution_inventory_fixture() as (_, _, _, settings), patch.object(
            distribution.setup, "discover_distribution_interpreters", side_effect=PermissionError(REMAINING_GUARD_FAILURE)):
        mutant = compile_distribution_guard_removal(distribution.distribute_fleet, (
            ('rows.append(distribution_surface_row("interpreter-discovery", "enumeration", reason=str(exc)))', 'pass'),))
        require_distribution_guard_mutant_killed(
            lambda: assert_remaining_capstone_gap("interpreter-discovery", settings),
            distribution, "distribute_fleet", mutant)


def test_distribution_denied_path_guard_removal():
    with distribution_inventory_fixture() as (_, _, _, settings), patch.dict(
            fleet.os.environ, {fleet.FLEET_TEST_ISOLATION_ENV: ""}), patch.object(
            fleet.subprocess, "run", return_value=SimpleNamespace(
                returncode=fleet.FLEET_WHICH_NO_INSTALLATIONS_EXIT_CODE, stdout="", stderr=REMAINING_GUARD_FAILURE)):
        mutant = compile_distribution_guard_removal(fleet.discover_host_spendguard_installations, (
            ('raise RuntimeError(f"PATH installation inventory failed: {result.stderr or result.stdout}")', 'return []'),))
        require_distribution_guard_mutant_killed(lambda: assert_remaining_capstone_gap("host installations", settings),
                                               fleet, "discover_host_spendguard_installations", mutant)


def test_distribution_unreadable_launcher_guard_removal():
    original = fleet.discover_host_spendguard_installations
    launcher_owner = SimpleNamespace(discover=original)
    mutant = compile_distribution_guard_removal(original, (
        ('raise RuntimeError(f"installation {cli_path} could not be inventoried: {exc}") from exc', 'continue'),))
    with distribution_inventory_fixture() as (root, _, _, settings):
        launcher = root / "unreadable-launcher"

        def reject_remaining_launcher_inventory():
            with patch.object(fleet, "discover_host_spendguard_installations", side_effect=lambda:
                              launcher_owner.discover(cli_paths=[launcher], pipx_document={})):
                assert_remaining_capstone_gap("host installations", settings)

        require_distribution_guard_mutant_killed(reject_remaining_launcher_inventory,
                                               launcher_owner, "discover", mutant)


def test_distribution_discovery_subguards_removal():
    cases = (
        ("walk", 'gaps.append({"surface": str(error.filename or scan_root), "reason": f"enumeration failed: {error}"})'),
        ("budget", 'gaps.append({"surface": str(root), "reason": "enumeration directory budget exhausted"})'),
        ("root", 'gaps.append({"surface": str(root), "reason": "discovery root missing"})'),
        ("venv", 'gaps.append({"surface": str(current), "reason": "venv interpreter missing"})'),
        ("interpreter", 'gaps.append({"surface": str(venv_path or executable), "reason": "interpreter missing"})'),
    )
    for axis, guard in cases:
        with distribution_inventory_fixture() as (root, _, workspace, settings), contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fleet, "registered_interpreters", return_value=[]))
            stack.enter_context(patch.object(fleet, "discover_host_spendguard_installations", return_value=[]))
            surface = workspace
            if axis == "walk":
                def fail_remaining_discovery_walk(scan_root, onerror):
                    onerror(PermissionError(None, REMAINING_GUARD_FAILURE, str(scan_root)))
                    return iter(())
                stack.enter_context(patch.object(setup.os, "walk", side_effect=fail_remaining_discovery_walk))
            elif axis == "budget":
                (workspace / "nested").mkdir()
                settings["distribution_scan_directory_budget"] = REMAINING_GUARD_BUDGET
            elif axis == "root":
                surface = root / "missing-root"
                settings["workspace_roots"] = [surface]
            elif axis == "venv":
                surface = workspace / "broken-venv"
                surface.mkdir()
                (surface / "pyvenv.cfg").touch()
            else:
                surface = root / "missing-python"
                stack.enter_context(patch.object(fleet, "registered_interpreters", return_value=[
                    {"python_executable": str(surface)}]))
            replacement = 'pass'
            if axis == "interpreter":
                # Remove the complete guard, including its early return.
                guard = 'if not executable.is_file():\n            ' + guard + '\n            return'
                replacement = 'if False:\n            return'
            mutant = compile_distribution_guard_removal(setup.discover_distribution_interpreters, ((guard, replacement),))
            require_distribution_guard_mutant_killed(lambda surface=surface: assert_remaining_capstone_gap(surface, settings),
                                                   setup, "discover_distribution_interpreters", mutant)
            print("MUTATION RED->GREEN discovery", axis)


def test_distribution_descriptor_pressure_guard_removal():
    with distribution_daemon_guard_fixture() as (_, rejects), patch.object(
            mcp_fd_lifecycle, "attest_mcp_process_descriptors", wraps=REMAINING_DESCRIPTOR_ATTESTOR), patch.object(
            mcp_fd_lifecycle, "mcp_descriptor_policy", return_value=(REMAINING_GUARD_LIMIT, REMAINING_GUARD_FRACTION, GUARD_REMOVAL_TIMEOUT)), patch.object(
            mcp_fd_lifecycle.psutil, "Process", return_value=SimpleNamespace(
                open_files=lambda: [], num_fds=lambda: REMAINING_GUARD_LIMIT)):
        mutant = compile_distribution_guard_removal(REMAINING_DESCRIPTOR_ATTESTOR, (
            ('descriptor_count < target * fraction', 'True'),))
        require_distribution_guard_mutant_killed(rejects, mcp_fd_lifecycle, "attest_mcp_process_descriptors", mutant)


def test_distribution_descriptor_inspection_guard_removal():
    with distribution_daemon_guard_fixture() as (_, rejects), patch.object(
            mcp_fd_lifecycle, "attest_mcp_process_descriptors", wraps=REMAINING_DESCRIPTOR_ATTESTOR), patch.object(
            mcp_fd_lifecycle.psutil, "Process", side_effect=PermissionError(REMAINING_GUARD_FAILURE)):
        mutant = compile_distribution_guard_removal(REMAINING_DESCRIPTOR_ATTESTOR, (
            ('return {"fd_ok": False, "reason": f"descriptor proof unavailable: {type(exc).__name__}: {exc}"}',
             'return {"fd_ok": True}'),))
        require_distribution_guard_mutant_killed(rejects, mcp_fd_lifecycle, "attest_mcp_process_descriptors", mutant)


def test_distribution_readonly_rpc_capability_guard_removal():
    original_rpc = distribution_daemon.request_distribution_mcp_rpc
    with distribution_daemon_guard_fixture() as (state, rejects):
        def answer_remaining_readonly_wire(request, timeout):
            if request.method == "HEAD":
                result = {}
            else:
                payload = json.loads(request.data)
                result = ({"serverInfo": {"version": GUARD_REMOVAL_VERSION}}
                          if payload["method"] == "initialize" else {"structuredContent": state["status"]})
            response = SimpleNamespace(headers={}, read=lambda: json.dumps({
                "id": distribution_daemon.DISTRIBUTION_RPC_ID, "jsonrpc": "2.0", "result": result}).encode())
            return contextlib.nullcontext(response)

        with patch.object(distribution_daemon, "request_distribution_mcp_rpc", original_rpc), patch.object(
                distribution_daemon, "urlopen", side_effect=answer_remaining_readonly_wire):
            mutant = compile_distribution_guard_removal(original_rpc, (
                ('if response.headers.get(MCP_READ_ONLY_ATTESTATION_HEADER) != MCP_READ_ONLY_ATTESTATION_HEADER_VALUE:',
                 'if False:'),))
            require_distribution_guard_mutant_killed(rejects, distribution_daemon, "request_distribution_mcp_rpc", mutant)


REMAINING_DESCRIPTOR_ATTESTOR = mcp_fd_lifecycle.attest_mcp_process_descriptors
REMAINING_GUARD_REGRESSIONS = (
    test_distribution_aggregation_unhealthy_guard_removal,
    test_distribution_aggregation_denied_inventory_guard_removal,
    test_distribution_denied_path_guard_removal,
    test_distribution_unreadable_launcher_guard_removal,
    test_distribution_discovery_subguards_removal,
    test_distribution_descriptor_pressure_guard_removal,
    test_distribution_descriptor_inspection_guard_removal,
    test_distribution_readonly_rpc_capability_guard_removal,
)

if __name__ == "__main__":
    for remaining_regression in REMAINING_GUARD_REGRESSIONS:
        remaining_regression()
        print("PASS red->green", remaining_regression.__name__)
