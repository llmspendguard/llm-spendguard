"""Offline inventory regressions: every invisible consumer must prevent green."""
import contextlib
import inspect
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
from spendguard import distribution, fleet, release, setup

INVENTORY_TARGET = {"sha": "inventory-current", "version": "inventory-current"}
INVENTORY_PROBE = dict(confirmed=True, version=INVENTORY_TARGET["version"],
                       metadata_version=INVENTORY_TARGET["version"], sha=INVENTORY_TARGET["sha"],
                       dirty=False, gate_enforcing=True)
INVENTORY_PID = 12345
INVENTORY_HEALTHY_PID = 12344


@contextlib.contextmanager
def distribution_inventory_fixture():
    with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
        root = Path(temporary)
        state = root / "state"
        state.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        settings = {"workspace_roots": [workspace], "consumer_audit_exempt": []}
        for owner, attribute, value in (
            (fleet.config, "HOME", state),
            (distribution, "resolve_distribution_target", INVENTORY_TARGET),
            (distribution, "distribution_policy", {}),
            (distribution.consumer_audit, "audit_consumer_workspaces", {"consumers": [], "pruned_at_depth": []}),
            (distribution, "probe_distribution_interpreter", INVENTORY_PROBE),
            (distribution, "attest_distribution_host", distribution.distribution_surface_row("host", "host", True, True)),
            (distribution, "attest_distribution_daemons", []),
            (release, "green_pointer", INVENTORY_TARGET),
        ):
            stack.enter_context(patch.object(owner, attribute, return_value=value) if callable(getattr(owner, attribute))
                                else patch.object(owner, attribute, value))
        yield root, state, workspace, settings


def assert_distribution_inventory_named_red(result, surface):
    assert result["exit_code"] != 0 and surface in result["summary"]["stragglers"], result


def test_distribution_external_directory_symlink_is_named_gap():
    with distribution_inventory_fixture() as (root, _state, workspace, settings):
        external = root / "external-stale-venv"
        (external / "bin").mkdir(parents=True)
        (external / "pyvenv.cfg").touch()
        (external / "bin" / "python").touch()
        linked = workspace / "linked-stale-venv"
        linked.symlink_to(external, target_is_directory=True)
        result = distribution.distribute_fleet(check=True, settings=settings)
        assert_distribution_inventory_named_red(result, str(linked))
        settings["consumer_audit_exempt"] = [linked]
        assert distribution.distribute_fleet(check=True, settings=settings)["exit_code"] == 0


def test_distribution_denied_pipx_inventory_is_named_gap():
    def answer_inventory_command(arguments, **_options):
        return SimpleNamespace(returncode=1, stdout="", stderr="inventory denied" if arguments[0] == "pipx" else "")

    with distribution_inventory_fixture() as (_root, _state, _workspace, settings), patch.dict(
            os.environ, {fleet.FLEET_TEST_ISOLATION_ENV: ""}), patch.object(
            fleet.subprocess, "run", side_effect=answer_inventory_command):
        assert_distribution_inventory_named_red(distribution.distribute_fleet(check=True, settings=settings),
                                                "host installations")


def test_distribution_check_empty_registry_is_read_only():
    with distribution_inventory_fixture() as (_root, state, _workspace, settings):
        before = set(state.rglob("*"))
        result = distribution.distribute_fleet(check=True, settings=settings)
        assert result["exit_code"] == 0 and result["summary"]["stragglers"] == [], result
        assert set(state.rglob("*")) == before, list(state.rglob("*"))
        with contextlib.closing(fleet._fleet_db()):
            pass
        existing = {path: path.read_bytes() for path in state.iterdir()}
        result = distribution.distribute_fleet(check=True, settings=settings)
        assert result["exit_code"] == 0, result
        assert {path: path.read_bytes() for path in state.iterdir()} == existing
        with contextlib.closing(fleet._fleet_db()) as writer:
            writer.execute("INSERT INTO consumers(pid,cmdline) VALUES(?,?)", (INVENTORY_PID, "api"))
            writer.commit()
            active_snapshot = {path: path.read_bytes() for path in state.iterdir()}
            with patch.object(fleet, "_classify_fleet_pid_identity", return_value=fleet._FLEET_PID_GONE):
                assert [row["pid"] for row in fleet.running_consumers(prune=False)] == [INVENTORY_PID]
                result = distribution.distribute_fleet(check=True, settings=settings)
            assert result["exit_code"] == 0, result
            assert {path: path.read_bytes() for path in state.iterdir()} == active_snapshot


def test_distribution_failed_consumer_row_is_named_gap():
    def classify_inventory_fixture_pid(pid, _created):
        if pid == INVENTORY_HEALTHY_PID:
            return fleet._FLEET_PID_VERIFIED_LIVE
        raise RuntimeError("stale API row unreadable")

    with distribution_inventory_fixture() as (_root, _state, _workspace, settings):
        with contextlib.closing(fleet._fleet_db()) as connection:
            connection.execute("INSERT INTO consumers(pid,version,served_sha,dirty,cmdline) VALUES(?,?,?,?,?)",
                               (INVENTORY_PID, "inventory-old", "inventory-old", 0, "api"))
            connection.execute("INSERT INTO consumers(pid,version,served_sha,dirty,cmdline) VALUES(?,?,?,?,?)",
                               (INVENTORY_HEALTHY_PID, INVENTORY_TARGET["version"], INVENTORY_TARGET["sha"], 0, "mcp"))
            connection.commit()
        with patch.object(fleet, "_classify_fleet_pid_identity", side_effect=classify_inventory_fixture_pid):
            result = distribution.distribute_fleet(check=True, settings=settings)
        assert_distribution_inventory_named_red(result, f"consumer pid:{INVENTORY_PID}")
        assert str(INVENTORY_PID) in next(row["straggler_reason"] for row in result["surfaces"]
                                        if row["surface"] == f"consumer pid:{INVENTORY_PID}"), result


def test_distribution_related_host_inventory_failures_are_not_empty():
    with distribution_inventory_fixture() as (root, _state, _workspace, settings):
        with patch.dict(os.environ, {fleet.FLEET_TEST_ISOLATION_ENV: ""}), patch.object(
                fleet.subprocess, "run", return_value=SimpleNamespace(returncode=2, stdout="", stderr="PATH denied")):
            assert_distribution_inventory_named_red(distribution.distribute_fleet(check=True, settings=settings),
                                                    "host installations")
        missing_cli = root / "unreadable-spendguard-cli"
        with patch.object(fleet, "discover_host_spendguard_installations", side_effect=lambda:
                          original_inventory_discovery(cli_paths=[missing_cli], pipx_document={})):
            result = distribution.distribute_fleet(check=True, settings=settings)
        assert_distribution_inventory_named_red(result, "host installations")
        assert str(missing_cli) in next(row["straggler_reason"] for row in result["surfaces"]
                                       if row["surface"] == "host installations")
        assert original_inventory_discovery(cli_paths=[], pipx_document={}) == []


original_inventory_discovery = fleet.discover_host_spendguard_installations


def test_distribution_inventory_guards_are_load_bearing():
    mutations = (
        (setup, "discover_distribution_interpreters", "if linked_directory.is_symlink():", "if False:",
         test_distribution_external_directory_symlink_is_named_gap),
        (fleet, "discover_host_spendguard_installations",
         'raise RuntimeError(f"pipx inventory failed: {result.stderr or result.stdout}")',
         'return []', test_distribution_denied_pipx_inventory_is_named_gap),
        (fleet, "_fleet_db", "if read_only", "if False",
         test_distribution_check_empty_registry_is_read_only),
        (fleet, "running_consumers",
         'raise inventory_error from exc',
         'pass', test_distribution_failed_consumer_row_is_named_gap),
    )
    for module, symbol, guard, replacement, regression in mutations:
        original = getattr(module, symbol)
        source = inspect.getsource(original)
        assert guard in source, (symbol, guard)
        namespace = dict(vars(module))
        exec(compile(source.replace(guard, replacement), inspect.getfile(original), "exec"), namespace)
        with patch.object(module, symbol, namespace[symbol]):
            try:
                regression()
            except AssertionError:
                print("MUTATION RED", regression.__name__)
            else:
                raise AssertionError(f"removed inventory guard survived: {symbol}")


if __name__ == "__main__":
    failures = []
    for regression in (test_distribution_external_directory_symlink_is_named_gap,
                       test_distribution_denied_pipx_inventory_is_named_gap,
                       test_distribution_check_empty_registry_is_read_only,
                       test_distribution_failed_consumer_row_is_named_gap,
                       test_distribution_related_host_inventory_failures_are_not_empty,
                       test_distribution_inventory_guards_are_load_bearing):
        try:
            regression()
            print("PASS", regression.__name__)
        except AssertionError as error:
            failures.append(regression.__name__)
            print("FAIL", regression.__name__, error)
    assert not failures, failures
