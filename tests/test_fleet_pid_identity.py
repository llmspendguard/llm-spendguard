"""Offline mutation guard: fleet actions require a recycle-proof (pid, create_time) identity."""
import os
import inspect
import sqlite3
import sys
import tempfile
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fleet-identity-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, fleet, release


class _FakeNoSuchProcess(Exception):
    pass


class _FakeProcess:
    def __init__(self, create_time):
        self._create_time = create_time

    def create_time(self):
        return self._create_time


VANISHED_PID = 91001
RECYCLED_PID = 91002
VERIFIED_STALE_PID = 91003
LEGACY_PID = 91004
RECORDED_CREATE_TIME = 1000.5
RECYCLED_CREATE_TIME = 9000.5
GREEN_SHA = "green-sha"
STALE_SHA = "stale-sha"
VANISHED_LABEL = "com.example.vanished"
RECYCLED_LABEL = "com.example.recycled"
VERIFIED_LABEL = "com.example.verified"
LEGACY_LABEL = "com.example.legacy"
NONFINITE_FLEET_CREATE_TIMES = ("nan", "inf", "-inf")


def assert_fleet_nonfinite_birth_proofs_are_unverifiable():
    for recorded_birth in NONFINITE_FLEET_CREATE_TIMES:
        with patch.object(fleet.psutil, "Process", return_value=_FakeProcess(RECORDED_CREATE_TIME)):
            assert fleet._classify_fleet_pid_identity(VERIFIED_STALE_PID, recorded_birth) == fleet._FLEET_PID_UNVERIFIABLE
    for observed_birth in NONFINITE_FLEET_CREATE_TIMES:
        with patch.object(fleet.psutil, "Process", return_value=_FakeProcess(float(observed_birth))):
            assert fleet._classify_fleet_pid_identity(VERIFIED_STALE_PID, RECORDED_CREATE_TIME) == fleet._FLEET_PID_UNVERIFIABLE


def test_fleet_nonfinite_birth_proofs_and_guard_mutation():
    assert_fleet_nonfinite_birth_proofs_are_unverifiable()
    original = fleet._classify_fleet_pid_identity
    source = inspect.getsource(original)
    guard = "        if not math.isfinite(recorded) or not math.isfinite(observed):\n            return _FLEET_PID_UNVERIFIABLE\n"
    assert guard in source
    namespace = dict(vars(fleet))
    exec(compile(source.replace(guard, ""), inspect.getfile(original), "exec"), namespace)
    with patch.object(fleet, original.__name__, namespace[original.__name__]):
        try:
            assert_fleet_nonfinite_birth_proofs_are_unverifiable()
        except AssertionError:
            return
    raise AssertionError("nonfinite fleet birth proof guard removal survived")


def _insert_consumer(pid, create_time, label):
    with sqlite3.connect(str(config.HOME / "fleet.db")) as connection:
        connection.execute(
            "INSERT INTO consumers(pid,served_sha,cmdline,pid_create_time,supervision_mode,supervision_ref) "
            "VALUES(?,?,?,?,?,?)",
            (pid, STALE_SHA, "fixture consumer", create_time, fleet.SUPERVISION_LAUNCHD, label),
        )


def test_fleet_pid_identity_controls_reporting_pruning_and_kickstarts():
    fleet._fleet_db().close()
    _insert_consumer(VANISHED_PID, repr(RECORDED_CREATE_TIME), VANISHED_LABEL)
    _insert_consumer(RECYCLED_PID, repr(RECORDED_CREATE_TIME), RECYCLED_LABEL)
    _insert_consumer(VERIFIED_STALE_PID, repr(RECORDED_CREATE_TIME), VERIFIED_LABEL)
    _insert_consumer(LEGACY_PID, None, LEGACY_LABEL)

    living = {
        RECYCLED_PID: _FakeProcess(RECYCLED_CREATE_TIME),
        VERIFIED_STALE_PID: _FakeProcess(RECORDED_CREATE_TIME),
        LEGACY_PID: _FakeProcess(RECORDED_CREATE_TIME),
    }
    kickstarts = []
    real_psutil = fleet.psutil
    real_green_pointer = release.green_pointer

    def fake_process(pid):
        if pid not in living:
            raise _FakeNoSuchProcess(pid)
        return living[pid]

    real_kickstart = fleet.kickstart_declared_launchd_consumer

    def fake_kickstart(label):
        kickstarts.append(label)
        return {"ok": True, "detail": ""}

    fake_psutil = type(
        "_FakePsutil",
        (),
        {"NoSuchProcess": _FakeNoSuchProcess, "Process": staticmethod(fake_process)},
    )
    fleet.psutil = fake_psutil
    fleet.kickstart_declared_launchd_consumer = fake_kickstart
    real_label_loaded = fleet.launchd_consumer_label_loaded
    fleet.launchd_consumer_label_loaded = lambda _label: False
    release.green_pointer = lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}
    try:
        consumers = fleet.running_consumers()
        by_pid = {consumer["pid"]: consumer for consumer in consumers}
        assert VANISHED_PID not in by_pid
        assert RECYCLED_PID not in by_pid
        assert by_pid[VERIFIED_STALE_PID]["identity_verified"] is True
        assert by_pid[VERIFIED_STALE_PID]["stale"] is True
        assert by_pid[LEGACY_PID]["identity_verified"] is False
        assert by_pid[LEGACY_PID]["stale"] is None

        with sqlite3.connect(str(config.HOME / "fleet.db")) as connection:
            remaining = {row[0] for row in connection.execute("SELECT pid FROM consumers")}
        assert VANISHED_PID not in remaining and RECYCLED_PID not in remaining

        fleet.launchd_consumer_label_loaded = lambda _label: True
        result = fleet.restart_stale(apply=True)
        targets = kickstarts
        assert any(VERIFIED_LABEL in target for target in targets), targets
        assert not any(VANISHED_LABEL in target for target in targets), targets
        assert not any(RECYCLED_LABEL in target for target in targets), targets
        assert not any(LEGACY_LABEL in target for target in targets), targets
        assert any(
            warning["pid"] == LEGACY_PID
            and warning["why"] == "stale but pid identity could not be verified — not kickstarting"
            for warning in result["warn"]
        ), result
        assert fleet.fleet_summary()["unverifiable"] == 1
    finally:
        fleet.psutil = real_psutil
        fleet.kickstart_declared_launchd_consumer = real_kickstart
        fleet.launchd_consumer_label_loaded = real_label_loaded
        release.green_pointer = real_green_pointer


if __name__ == "__main__":
    cases = [case for name, case in sorted(globals().items()) if name.startswith("test_") and callable(case)]
    for case in cases:
        case()
        print(f"  [OK] {case.__name__}")
    print(f"\nOK test_fleet_pid_identity: {len(cases)} test(s) passed")
