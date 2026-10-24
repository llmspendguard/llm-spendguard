"""Offline Phase-1 fleet control-plane guarantees; never touches real launchd or the real registry."""
import os
import pathlib
import sqlite3
import tempfile
import contextlib
import io

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fleet-supervision-")

from spendguard import cli, config, fleet, release  # noqa: E402
from spendguard import codex_broker_owner, launchd_labels  # noqa: E402

config.HOME = pathlib.Path(os.environ["SPENDGUARD_HOME"])

TEST_PID = os.getpid()
TEST_CREATE_TIME = repr(fleet.psutil.Process(TEST_PID).create_time())
OLD_SHA = "old-sha"
GREEN_SHA = "green-sha"
TEST_LABEL = "com.example.supervised"


def _reset_registry():
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    database_path = config.HOME / "fleet.db"
    if database_path.exists():
        database_path.unlink()


def _insert_consumer(*, mode=None, ref=None, role=fleet.CONSUMER_ROLE_DAEMON,
                     pid=TEST_PID, create_time=TEST_CREATE_TIME, served_sha=OLD_SHA):
    fleet._fleet_db().close()
    with sqlite3.connect(config.HOME / "fleet.db") as connection:
        connection.execute(
            "INSERT OR REPLACE INTO consumers"
            "(pid,served_sha,served_short,cmdline,pid_create_time,role,supervision_mode,supervision_ref) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (pid, served_sha, served_sha, "fixture", create_time, role, mode, ref),
        )


def test_supervision_schema_fresh_and_idempotent_migration():
    _reset_registry()
    fleet._fleet_db().close()
    with sqlite3.connect(config.HOME / "fleet.db") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(consumers)")}
    assert {fleet.CONSUMER_SUPERVISION_MODE_COLUMN, fleet.CONSUMER_SUPERVISION_REF_COLUMN} <= columns

    (config.HOME / "fleet.db").unlink()
    with sqlite3.connect(config.HOME / "fleet.db") as connection:
        connection.execute(
            "CREATE TABLE consumers(pid INTEGER PRIMARY KEY, version TEXT, served_sha TEXT, served_short TEXT, "
            "dirty INTEGER, cmdline TEXT, launchd_label TEXT, registered_ts TEXT, venv_root TEXT, "
            "python_executable TEXT, pid_create_time TEXT, role TEXT)"
        )
    fleet._fleet_db().close()
    fleet._fleet_db().close()
    with sqlite3.connect(config.HOME / "fleet.db") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(consumers)")}
    assert {fleet.CONSUMER_SUPERVISION_MODE_COLUMN, fleet.CONSUMER_SUPERVISION_REF_COLUMN} <= columns


def test_declared_launchd_registration_round_trips_and_legacy_is_unsupervised():
    _reset_registry()
    release.served_sha = lambda: {"sha": OLD_SHA, "short": OLD_SHA, "dirty": False}
    release.green_pointer = lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}
    fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON,
                            supervision_mode=fleet.SUPERVISION_LAUNCHD,
                            supervision_ref=TEST_LABEL)
    registered = next(row for row in fleet.running_consumers() if row["pid"] == TEST_PID)
    assert registered["supervision_mode"] == fleet.SUPERVISION_LAUNCHD
    assert registered["supervision_ref"] == TEST_LABEL
    assert registered["classification"] == "stale"

    _reset_registry()
    _insert_consumer(role=fleet.CONSUMER_ROLE_UNSPECIFIED)
    legacy = fleet.running_consumers()[0]
    assert legacy["classification"] == "unsupervised"
    assert fleet.restart_stale()["warn"]


def test_dry_run_apply_failure_and_kill_switch(monkeypatch):
    _reset_registry()
    _insert_consumer(mode=fleet.SUPERVISION_LAUNCHD, ref=TEST_LABEL)
    release.green_pointer = lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}
    calls = []
    monkeypatch.setattr(fleet, "kickstart_declared_launchd_consumer",
                        lambda label: calls.append(label) or {"ok": True, "detail": "kickstarted"})
    monkeypatch.setattr(fleet, "verify_launchd_consumer_green_return", lambda **_kwargs: False)

    dry = fleet.restart_stale(apply=False)
    assert not calls and dry["restarted"][0]["action"] == "would kickstart"
    applied = fleet.restart_stale(apply=True)
    assert calls == [TEST_LABEL]
    assert applied["failed"] and not applied["confirmed"]

    calls.clear()
    monkeypatch.setenv(fleet.FLEET_RESTARTS_ENABLED_ENV, "0")
    disabled = fleet.restart_stale(apply=True)
    assert not calls and disabled["warn"] and disabled["disabled"] is True


def test_recycled_identity_never_restarts_or_stops(monkeypatch):
    _reset_registry()
    recycled_time = str(float(TEST_CREATE_TIME) - 100.0)
    _insert_consumer(mode=fleet.SUPERVISION_LAUNCHD, ref=TEST_LABEL, create_time=recycled_time)
    release.green_pointer = lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}
    calls = []
    monkeypatch.setattr(fleet, "kickstart_declared_launchd_consumer", lambda label: calls.append(label))
    monkeypatch.setattr(fleet, "bootout_declared_launchd_consumer", lambda label: calls.append(label))
    fleet.restart_stale(apply=True)
    stopped = fleet.stop_supervised_consumer(TEST_PID, apply=True)
    assert not calls
    assert stopped["ok"] is False


def test_fleet_consumers_cli_and_label_single_sources(monkeypatch):
    _reset_registry()
    _insert_consumer(mode=fleet.SUPERVISION_LAUNCHD, ref=TEST_LABEL)
    release.green_pointer = lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli._dispatch(["fleet", "consumers"]) == 0
    rendered = output.getvalue()
    assert "supervision=launchd" in rendered
    assert "classification=stale" in rendered
    restart_calls = []
    monkeypatch.setattr(
        fleet,
        "restart_stale",
        lambda **kwargs: restart_calls.append(kwargs) or {"restarted": [], "failed": [], "warn": []},
    )
    assert cli._dispatch(["fleet", "restart", "--stale"]) == 0
    assert restart_calls == [{"apply": False, "consumer_id": None, "all_supervised": False}]
    assert cli._dispatch(["fleet", "restart", "--stale", "--apply"]) == 0
    assert restart_calls[-1] == {"apply": True, "consumer_id": None, "all_supervised": False}
    assert (codex_broker_owner.CODEX_BROKER_OWNER_LAUNCHD_LABEL
            == launchd_labels.CODEX_BROKER_OWNER_LAUNCHD_LABEL)
    installer = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "lane" / "install_lane_drainer.sh"
    assert "from spendguard.launchd_labels import LANE_DRAIN_LAUNCHD_LABEL" in installer.read_text()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        if "monkeypatch" in test.__code__.co_varnames:
            from pytest import MonkeyPatch
            patcher = MonkeyPatch()
            try:
                test(patcher)
            finally:
                patcher.undo()
        else:
            test()
        print(f"  [OK] {test.__name__}")
    print(f"OK test_fleet_launchd_supervision: {len(tests)} test(s) passed")
