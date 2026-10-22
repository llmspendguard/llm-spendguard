"""Offline mutation guard: fleet actions require a recycle-proof (pid, create_time) identity."""
import os
import sqlite3
import subprocess
import sys
import tempfile

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


def _insert_consumer(pid, create_time, label):
    with sqlite3.connect(str(config.HOME / "fleet.db")) as connection:
        connection.execute(
            "INSERT INTO consumers(pid,served_sha,cmdline,launchd_label,pid_create_time) VALUES(?,?,?,?,?)",
            (pid, STALE_SHA, "fixture consumer", label, create_time),
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
    real_subprocess_run = subprocess.run
    real_green_pointer = release.green_pointer

    def fake_process(pid):
        if pid not in living:
            raise _FakeNoSuchProcess(pid)
        return living[pid]

    def fake_subprocess_run(argv, **kwargs):
        if argv[:3] == ["launchctl", "kickstart", "-k"]:
            kickstarts.append(list(argv))
        return type("_FakeCompletedProcess", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    fake_psutil = type(
        "_FakePsutil",
        (),
        {"NoSuchProcess": _FakeNoSuchProcess, "Process": staticmethod(fake_process)},
    )
    fleet.psutil = fake_psutil
    subprocess.run = fake_subprocess_run
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

        result = fleet.restart_stale(apply=True)
        targets = [argv[-1] for argv in kickstarts]
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
        subprocess.run = real_subprocess_run
        release.green_pointer = real_green_pointer


if __name__ == "__main__":
    cases = [case for name, case in sorted(globals().items()) if name.startswith("test_") and callable(case)]
    for case in cases:
        case()
        print(f"  [OK] {case.__name__}")
    print(f"\nOK test_fleet_pid_identity: {len(cases)} test(s) passed")
