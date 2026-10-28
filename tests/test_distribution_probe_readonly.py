"""Real interpreter startup must never persist distribution observation state."""
import contextlib
import atexit
import io
import json
import logging
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch
from types import SimpleNamespace

PROBE_READONLY_HOME_PREFIX = "sg-probe-readonly-"
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix=PROBE_READONLY_HOME_PREFIX)
    os.execv(sys.executable, [sys.executable] + sys.argv)

PROBE_READONLY_INITIAL_HOME = tempfile.mkdtemp()
os.environ["SPENDGUARD_HOME"] = PROBE_READONLY_INITIAL_HOME
atexit.register(shutil.rmtree, PROBE_READONLY_INITIAL_HOME, ignore_errors=True)
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import distribution  # noqa: E402
from spendguard import _version  # noqa: E402
from spendguard import fleet  # noqa: E402

PROBE_READONLY_BOOTSTRAP_NAME = "sitecustomize.py"
PROBE_READONLY_BOOTSTRAP = "import spendguard; spendguard.install()\n"
PROBE_READONLY_TRANSIENT_STATE_NAME = "transient-state.json"


def exercise_distribution_probe_startup_without_persistence(disabled=False, runtime_socket=False):
    with tempfile.TemporaryDirectory(dir=os.environ.get("SPENDGUARD_GATE_TMPDIR_BASE", tempfile.gettempdir())) as temporary:
        root = Path(temporary)
        state = root / "state"
        state.mkdir()
        runtime_endpoint = socket.socket(socket.AF_UNIX) if runtime_socket else None
        if runtime_endpoint is not None:
            runtime_endpoint.bind(str(state / "runtime.sock"))
        if disabled:
            (state / distribution.config.FLAG.relative_to(distribution.config.HOME)).touch()
        original_state = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
        original_entries = {path.relative_to(state) for path in state.rglob("*")}
        environment = root / "interpreter"
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
        site = next(environment.glob("lib/python*/site-packages"))
        (site / "distribution-source.pth").write_text(str(Path(distribution.__file__).resolve().parents[1]) + "\n")
        (site / PROBE_READONLY_BOOTSTRAP_NAME).write_text(PROBE_READONLY_BOOTSTRAP)
        metadata = site / ("llm_spendguard-" + _version.__version__ + ".dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: llm-spendguard\nVersion: " + _version.__version__ + "\n")
        python = environment / "bin" / "python"
        with patch.object(distribution.config, "HOME", state), patch.dict(os.environ, {
                "SPENDGUARD_HOME": str(state), "SPENDGUARD_NO_AUTOINSTALL": "1",
                "SPENDGUARD_TEST_ISOLATED": "1"}, clear=False):
            proof = distribution.probe_distribution_interpreter(python, {
                "probe_timeout_s": distribution.DISTRIBUTION_PROBE_TIMEOUT_S})
            assert proof.get("confirmed"), proof
            assert proof.get("gate_enforcing") is not disabled, proof
            with contextlib.ExitStack() as stack:
                for attribute, value in (
                    ("resolve_distribution_target", dict(version=proof["version"], sha=proof["sha"])),
                    ("attest_distribution_host", distribution.distribution_surface_row("host", "host", True, True)),
                    ("attest_distribution_api_consumers", []), ("attest_distribution_daemons", []),
                ):
                    stack.enter_context(patch.object(distribution, attribute, return_value=value))
                stack.enter_context(patch.object(distribution.setup, "discover_distribution_interpreters",
                    return_value={"interpreters": [{"python": str(python), "kind": "venv", "venv": str(environment)}], "gaps": []}))
                stack.enter_context(patch.object(distribution.config, "consumer_audit_settings", return_value={}))
                stack.enter_context(patch.object(distribution.consumer_audit, "audit_consumer_workspaces",
                    return_value={"consumers": [], "pruned_at_depth": []}))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    distribution.distribute_command(["--check", "--json"])
                rows = json.loads(output.getvalue())["surfaces"]
                interpreter_rows = [row for row in rows if row["surface"] == str(python)]
                assert len(interpreter_rows) == 1, rows
                assert interpreter_rows[0]["probe"]["confirmed"], rows
            observed_state = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
            assert observed_state == original_state, list(state.iterdir())
            assert {path.relative_to(state) for path in state.rglob("*")} == original_entries
        if runtime_endpoint is not None:
            runtime_endpoint.close()


def test_distribution_probe_snapshot_tolerates_source_entry_copy_race():
    with tempfile.TemporaryDirectory() as temporary_directory:
        state = Path(temporary_directory) / "state"
        state.mkdir()
        transient_state = state / PROBE_READONLY_TRANSIENT_STATE_NAME
        transient_state.touch()
        snapshot = Path(temporary_directory) / "snapshot"
        real_copy2 = shutil.copy2

        def remove_distribution_fixture_source_during_copy(source, destination):
            transient_state.unlink()
            return real_copy2(source, destination)

        with patch.object(distribution.shutil, "copy2", side_effect=remove_distribution_fixture_source_during_copy):
            with unittest.TestCase().assertLogs(distribution.__name__, level=logging.WARNING) as records:
                distribution.copy_distribution_snapshot_entry(transient_state, snapshot)
        assert not snapshot.exists()
        assert str(transient_state) in records.output[0]


def test_distribution_probe_snapshot_copy_race_fails_closed_for_present_source():
    with tempfile.TemporaryDirectory() as temporary_directory:
        state = Path(temporary_directory) / "state"
        state.mkdir()
        persistent_state = state / PROBE_READONLY_TRANSIENT_STATE_NAME
        persistent_state.touch()
        snapshot = Path(temporary_directory) / "snapshot"

        with patch.object(distribution.shutil, "copy2", side_effect=FileNotFoundError):
            try:
                distribution.copy_distribution_snapshot_entry(persistent_state, snapshot)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("still-present snapshot source did not fail closed")


test_distribution_probe_snapshot_tolerates_source_entry_copy_race()
test_distribution_probe_snapshot_copy_race_fails_closed_for_present_source()


def test_distribution_probe_snapshot_is_load_bearing():
    # Actual unisolated startup reproduces the original fleet.db write. Removing
    # only the environment guard must turn the same no-persistence assertion red.
    with patch.object(distribution, "distribution_probe_environment",
                      lambda: contextlib.nullcontext(distribution.distribution_clean_environment())):
        try:
            exercise_distribution_probe_startup_without_persistence()
        except AssertionError as error:
            assert "fleet.db" in str(error), error
            print("MUTATION RED: real probe startup persists fleet.db without state isolation")
        else:
            raise AssertionError("distribution probe state isolation removal survived")
    exercise_distribution_probe_startup_without_persistence()
    exercise_distribution_probe_startup_without_persistence(disabled=True)
    exercise_distribution_probe_startup_without_persistence(runtime_socket=True)


def test_distribution_probe_absolute_ledgers_are_disposable():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state = root / "state"
        state.mkdir()
        ledger = root / "external.db"
        with sqlite3.connect(ledger) as source_connection:
            source_connection.execute("PRAGMA journal_mode=WAL")
            source_connection.execute("PRAGMA wal_autocheckpoint=0")
            source_connection.execute("CREATE TABLE proof(value TEXT)")
            source_connection.execute("INSERT INTO proof VALUES (?)", (str(root),))
        assert Path(str(ledger) + "-wal").exists()
        policy_path = state / distribution.config.CONFIG_JSON.name
        policy_path.write_text(json.dumps({"budget": {"db_path": str(ledger)}}))
        original = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        with patch.object(distribution.config, "HOME", state):
            with distribution.distribution_probe_environment() as environment:
                snapshot = Path(environment["SPENDGUARD_HOME"])
                policy = json.loads((snapshot / policy_path.name).read_text())
                cloned_ledger = Path(policy["budget"]["db_path"])
                assert cloned_ledger != ledger and snapshot in cloned_ledger.parents
                with sqlite3.connect(cloned_ledger) as connection:
                    assert connection.execute("SELECT value FROM proof").fetchone() == (str(root),)
                    connection.execute("INSERT INTO proof VALUES (?)", (str(snapshot),))
        assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == original
        source_connection.close()


def test_fleet_registry_snapshot_access_time_and_content_evidence():
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary)
        registry = state / "fleet.db"
        with sqlite3.connect(registry) as connection:
            connection.execute("CREATE TABLE snapshot_proof(value TEXT)")
            connection.execute("INSERT INTO snapshot_proof VALUES (?)", (temporary,))
        original_stat = Path.stat
        original_read = Path.read_bytes
        stat_observations = []

        def fleet_snapshot_access_time_only(path, *args, **kwargs):
            observed = original_stat(path, *args, **kwargs)
            if path == registry:
                attributes = {name: getattr(observed, name) for name in dir(observed)
                              if name.startswith("st_")}
                attributes["st_atime"] += len(stat_observations)
                attributes["st_atime_ns"] += len(stat_observations)
                stat_observations.append(attributes["st_atime_ns"])
                return SimpleNamespace(**attributes)
            return observed

        with patch.object(fleet.config, "HOME", state), patch.object(Path, "stat", fleet_snapshot_access_time_only):
            snapshot = fleet._fleet_db(read_only=True)
            try:
                assert snapshot.execute("SELECT value FROM snapshot_proof").fetchone() == (temporary,)
            finally:
                snapshot.close()
        assert len(set(stat_observations)) > 1
        content_observations = []

        def fleet_snapshot_changed_content(path):
            content = original_read(path)
            if path == registry:
                content_observations.append(content)
                if len(content_observations) > 1:
                    return content + temporary.encode()
            return content

        with patch.object(fleet.config, "HOME", state), patch.object(Path, "read_bytes", fleet_snapshot_changed_content):
            try:
                fleet._fleet_db(read_only=True)
            except RuntimeError as error:
                assert str(error) == "consumer registry changed during read-only snapshot"
            else:
                raise AssertionError("registry content change escaped snapshot consistency guard")
        assert len(content_observations) > 1


if __name__ == "__main__":
    exercise_distribution_probe_startup_without_persistence()
    test_distribution_probe_snapshot_is_load_bearing()
    test_distribution_probe_absolute_ledgers_are_disposable()
    test_fleet_registry_snapshot_access_time_and_content_evidence()
    print("PASS real armed interpreter probe: no persistent writes; isolation removal red")
