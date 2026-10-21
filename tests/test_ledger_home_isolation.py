"""Reproduce late isolation after gated startup, without accessing any user home.

The child starts with a disposable original home, just as sitecustomize starts
with the user's home. It then changes only the environment, as the leaking
fixture did. Every attempted write must refuse before changing original bytes.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

LEDGER_ISOLATION_CHILD_TIMEOUT_S = 60
LEDGER_ALIGNMENT_OPERATION_REPETITIONS = 32
LEDGER_SHORTCUT_NATIVE_STEP_ALIGNMENT_CHECKS = 1
LEDGER_ALIGNMENT_PROBE_SCHEMA = "CREATE TABLE alignment_probe (value)"
LEDGER_ALIGNMENT_PROBE_INSERT = "INSERT INTO alignment_probe VALUES (?)"
LEDGER_ALIGNMENT_PROBE_READ = "SELECT * FROM alignment_probe"
LEDGER_ISOLATION_HELD_WRITER_CASES = (
    "held_ledger", "held_connection", "held_cursor", "held_many", "held_script",
    "held_commit", "held_context", "held_shortcut_cursor",
)
LEDGER_ISOLATION_WRITER_CASES = (
    "calls", "ledger", "explicit", "pooled", *LEDGER_ISOLATION_HELD_WRITER_CASES,
)
LEDGER_ISOLATION_CHILD_SOURCE = r'''
import os
import pathlib
import sys
from spendguard import calls, config
from spendguard.ledger import SpendLedger

original = pathlib.Path(os.environ["SPENDGUARD_HOME"])
isolated = pathlib.Path(sys.argv[1])
writer = sys.argv[2]
assert config.HOME == original, "startup must cache the original disposable home"
# Substitute only the protected default's identity; all actual paths stay temporary.
config.DEFAULT_SPENDGUARD_HOME = original
cached_path = config.db_path()
pool_key = __name__
zero_cost = 0
event = {"source": __name__, "model": __name__, "cost_basis": "unpriced"}
fixture_insert_sql = "INSERT INTO spend_events (id, source, cost_basis) VALUES (?, ?, ?)"
fixture_insert_parameters = (__name__, __name__, "unpriced")
held_writer_cases = tuple(sys.argv[3:])
if writer == "pooled":
    config.pooled_ledger_conn(pool_key, lambda connection: None)
if writer in held_writer_cases:
    held_ledger = SpendLedger(cached_path)
    held_cursor = held_ledger._conn.cursor()
    if writer == "held_shortcut_cursor":
        held_cursor = held_ledger._conn.execute("SELECT * FROM spend_events")
    if writer in ("held_commit", "held_context"):
        held_ledger._conn.execute(fixture_insert_sql, fixture_insert_parameters)

# Include WAL and all auxiliary files: refusal may not even initialize a schema.
before = {p.relative_to(original): p.read_bytes()
          for p in original.rglob("*") if p.is_file()}
os.environ["SPENDGUARD_HOME"] = str(isolated)
refused = False
try:
    if writer == "calls":
        calls.record_call(__name__, __name__, "realtime", zero_cost)
    elif writer == "pooled":
        config.pooled_ledger_conn(pool_key, lambda connection: None)
    elif writer == "held_ledger":
        held_ledger.record_event(event)
    elif writer == "held_connection":
        held_ledger._conn.execute(fixture_insert_sql, fixture_insert_parameters)
    elif writer in ("held_cursor", "held_shortcut_cursor"):
        held_cursor.execute(fixture_insert_sql, fixture_insert_parameters)
    elif writer == "held_many":
        held_cursor.executemany(fixture_insert_sql, (fixture_insert_parameters,))
    elif writer == "held_script":
        held_ledger._conn.executescript("INSERT INTO spend_events DEFAULT VALUES;")
    elif writer == "held_commit":
        held_ledger._conn.commit()
    elif writer == "held_context":
        with held_ledger._conn:
            pass
    else:
        ledger = SpendLedger(cached_path if writer == "explicit" else None)
        try:
            ledger.record_event(event)
        finally:
            ledger._conn.close()
except BaseException as error:
    assert type(error).__name__ == "LedgerHomeIsolationError", repr(error)
    refused = True
assert refused, f"{writer} silently used the startup home after isolation changed"
after = {p.relative_to(original): p.read_bytes()
         for p in original.rglob("*") if p.is_file()}
assert before == after, "refusal must preserve all original ledger bytes"
'''


@pytest.mark.parametrize("writer_case", LEDGER_ISOLATION_WRITER_CASES)
def test_late_home_change_refuses_startup_ledger(writer_case):
    """Fault: startup cached a different home; include an already-pooled handle."""
    with tempfile.TemporaryDirectory(prefix=__name__) as original_home:
        with tempfile.TemporaryDirectory(prefix=__name__) as isolated_home:
            child_env = dict(os.environ)
            child_env["SPENDGUARD_HOME"] = original_home
            child_env["SPENDGUARD_CALLS"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", LEDGER_ISOLATION_CHILD_SOURCE, isolated_home,
                 writer_case, *LEDGER_ISOLATION_HELD_WRITER_CASES],
                env=child_env, capture_output=True, text=True,
                timeout=LEDGER_ISOLATION_CHILD_TIMEOUT_S,
            )
            assert result.returncode == 0, (writer_case, result.stdout, result.stderr)


def test_aligned_home_keeps_fixture_events_isolated(tmp_path):
    """Containment must still allow the normal explicitly isolated writer."""
    from spendguard.ledger import SpendLedger

    fixture_path = tmp_path / pathlib.Path(__file__).stem
    fixture_ledger = SpendLedger(fixture_path)
    try:
        fixture_event_id = fixture_ledger.record_event(
            {"source": __name__, "cost_basis": "unpriced"})
        assert fixture_ledger.get(fixture_event_id)["source"] == __name__
    finally:
        fixture_ledger._conn.close()


def test_stable_home_operations_do_not_resolve_paths(tmp_path, monkeypatch):
    """Fault: filesystem resolution is unavailable after a protected home warms."""
    from spendguard import config

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_SPENDGUARD_HOME", tmp_path)
    monkeypatch.setenv(config.LEDGER_HOME_ENVIRONMENT_KEY, str(tmp_path))
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    try:
        connection.execute(LEDGER_ALIGNMENT_PROBE_SCHEMA)
        retained_cursor = connection.cursor()
        config.require_ledger_home_alignment(tmp_path)

        def reject_repeated_alignment_resolution(*args, **kwargs):
            raise AssertionError("stable ledger operation repeated filesystem resolution")

        with monkeypatch.context() as resolution_fault:
            resolution_fault.setattr(pathlib.Path, "resolve", reject_repeated_alignment_resolution)
            for _ in range(LEDGER_ALIGNMENT_OPERATION_REPETITIONS):
                connection.execute(LEDGER_ALIGNMENT_PROBE_READ).fetchone()
                retained_cursor.execute(LEDGER_ALIGNMENT_PROBE_READ).fetchone()
                retained_cursor.executemany(LEDGER_ALIGNMENT_PROBE_INSERT, ((__name__,),))
                connection.commit()
                with connection:
                    pass
        monkeypatch.setenv(config.LEDGER_HOME_ENVIRONMENT_KEY, str(tmp_path.parent))
        with pytest.raises(config.LedgerHomeIsolationError):
            retained_cursor.execute(LEDGER_ALIGNMENT_PROBE_READ)
    finally:
        connection.close()


def test_redirected_home_cursor_uses_bound_open_identity(tmp_path, monkeypatch):
    """Fault: resolution fails after an unrelated target opens during a redirect."""
    from spendguard import config

    protected_home = tmp_path / test_redirected_home_cursor_uses_bound_open_identity.__name__
    monkeypatch.setattr(config, "HOME", protected_home)
    monkeypatch.setattr(config, "DEFAULT_SPENDGUARD_HOME", protected_home)
    monkeypatch.setenv(config.LEDGER_HOME_ENVIRONMENT_KEY, str(tmp_path))
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    try:
        connection.execute(LEDGER_ALIGNMENT_PROBE_SCHEMA)
        retained_cursor = connection.cursor()

        def reject_redirected_cursor_reresolution(*args, **kwargs):
            raise AssertionError("opened ledger identity must not be resolved again")

        original_environment_get = os.environ.get

        def reject_absolute_cursor_expansion_dependency(key, *args):
            if key == config.LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY:
                raise AssertionError("absolute ledger identity must not depend on tilde expansion")
            return original_environment_get(key, *args)

        with monkeypatch.context() as resolution_fault:
            resolution_fault.setattr(pathlib.Path, "resolve", reject_redirected_cursor_reresolution)
            resolution_fault.setattr(os, "getcwd", reject_redirected_cursor_reresolution)
            resolution_fault.setattr(os.path, "isabs", reject_redirected_cursor_reresolution)
            resolution_fault.setattr(os.environ, "get", reject_absolute_cursor_expansion_dependency)
            for _ in range(LEDGER_ALIGNMENT_OPERATION_REPETITIONS):
                retained_cursor.executemany(LEDGER_ALIGNMENT_PROBE_INSERT, ((__name__,),))
                connection.execute(LEDGER_ALIGNMENT_PROBE_READ).fetchall()
                connection.commit()
    finally:
        connection.close()


def test_relative_home_chdir_rechecks_retained_cursor(tmp_path, monkeypatch):
    """Fault: a relative redirect changes identity while the cursor remains live."""
    from spendguard import config

    protected_home = tmp_path / test_relative_home_chdir_rechecks_retained_cursor.__name__
    protected_home.mkdir()
    monkeypatch.setattr(config, "HOME", protected_home)
    monkeypatch.setattr(config, "DEFAULT_SPENDGUARD_HOME", protected_home)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(config.LEDGER_HOME_ENVIRONMENT_KEY, protected_home.name)
    connection = config.open_ledger_connection(protected_home / pathlib.Path(__file__).stem)
    try:
        connection.execute(LEDGER_ALIGNMENT_PROBE_SCHEMA)
        retained_cursor = connection.cursor()
        retained_cursor.execute(LEDGER_ALIGNMENT_PROBE_READ).fetchone()
        monkeypatch.chdir(tmp_path.parent)
        with pytest.raises(config.LedgerHomeIsolationError):
            retained_cursor.execute(LEDGER_ALIGNMENT_PROBE_READ)
    finally:
        connection.close()


def test_connection_shortcut_checks_alignment_once_before_native_step(tmp_path, monkeypatch):
    """Count the mandatory guard while stale-home refusal remains fault tested."""
    from spendguard import config

    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    alignment_checks = []
    original_alignment_guard = config.require_ledger_home_alignment

    def observe_shortcut_native_alignment_check(*args, **kwargs):
        alignment_checks.append(args)
        return original_alignment_guard(*args, **kwargs)

    try:
        monkeypatch.setattr(config, "require_ledger_home_alignment", observe_shortcut_native_alignment_check)
        connection.execute(LEDGER_ALIGNMENT_PROBE_SCHEMA)
        assert len(alignment_checks) == LEDGER_SHORTCUT_NATIVE_STEP_ALIGNMENT_CHECKS
    finally:
        connection.close()


def test_alignment_cache_bypass_mutation_is_rejected(tmp_path, monkeypatch):
    """Reintroduce per-operation resolution; the filesystem fault must catch it."""
    from spendguard import config

    monkeypatch.setattr(config, "_resolved_ledger_home_redirect",
                        config._resolved_ledger_home_redirect.__wrapped__)
    with pytest.raises(AssertionError, match="stable ledger operation repeated filesystem resolution"):
        test_stable_home_operations_do_not_resolve_paths(tmp_path, monkeypatch)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
