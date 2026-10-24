"""Offline guards for the operator orphan-reaper surface and its periodic fleet registration."""
import contextlib
import io
import json
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reap-cli-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import cli, fleet, warm_stdio_daemon

TEST_LANE_LABEL = "codex"
TEST_DEAD_OWNER_PID = 8201
TEST_LIVE_OWNER_PID = 8202
TEST_ORPHAN_SERVER_PID = 7201
TEST_LIVE_SERVER_PID = 7202
TEST_UNREADABLE_PROCESS_PID = 7203
TEST_REAP_ERROR = "reap(7201): group still alive after SIGKILL grace"
TEST_CROSS_UID_SKIPPED = 245


def _operator_reap_report(*, execute, error=False):
    return {"scanned": 2, "orphans": [TEST_ORPHAN_SERVER_PID],
            "reaped": [TEST_ORPHAN_SERVER_PID] if execute and not error else [],
            "cross_uid_skipped": TEST_CROSS_UID_SKIPPED,
            "unreadable": [f"{TEST_UNREADABLE_PROCESS_PID}: RuntimeError"],
            "errors": [TEST_REAP_ERROR] if error else [],
            "marked": [
                {"pid": TEST_ORPHAN_SERVER_PID, "lane": TEST_LANE_LABEL,
                 "owner_pid": TEST_DEAD_OWNER_PID, "owner_state": "dead", "orphan": True},
                {"pid": TEST_LIVE_SERVER_PID, "lane": TEST_LANE_LABEL,
                 "owner_pid": TEST_LIVE_OWNER_PID, "owner_state": "alive", "orphan": False},
            ]}


def _invoke_reap_orphans_cli(arguments, report):
    original_reaper = warm_stdio_daemon.reap_orphaned_warm_servers
    calls = []

    def recorded_reaper(lane=None, execute=True):
        calls.append((lane, execute))
        return report

    warm_stdio_daemon.reap_orphaned_warm_servers = recorded_reaper
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            return_code = cli.reap_orphaned_warm_servers_cli(arguments)
    finally:
        warm_stdio_daemon.reap_orphaned_warm_servers = original_reaper
    return return_code, output.getvalue(), calls


def test_reap_orphans_cli_dry_run_is_default_and_surfaces_owner_counts_and_unreadable():
    return_code, output, calls = _invoke_reap_orphans_cli([], _operator_reap_report(execute=False))
    assert return_code == 0
    assert calls == [(None, False)]
    assert "2 marked server(s), 1 provably-dead orphan(s)" in output
    assert f"owner {TEST_LIVE_OWNER_PID}: alive" in output
    assert f"unreadable: {TEST_UNREADABLE_PROCESS_PID}: RuntimeError" in output
    assert f"{TEST_CROSS_UID_SKIPPED} cross-uid processes skipped" in output
    assert "would reap: 1" in output


def test_reap_orphans_cli_execute_json_passes_lane_and_emits_valid_machine_report():
    report = _operator_reap_report(execute=True)
    return_code, output, calls = _invoke_reap_orphans_cli(
        ["--execute", "--lane", TEST_LANE_LABEL, "--json"], report)
    payload = json.loads(output)
    assert return_code == 0
    assert calls == [(TEST_LANE_LABEL, True)]
    assert payload["mode"] == "execute"
    assert payload["reaped"] == [TEST_ORPHAN_SERVER_PID]
    assert payload["unreadable"]
    assert payload["cross_uid_skipped"] == TEST_CROSS_UID_SKIPPED


def test_reap_orphans_cli_caps_genuine_unreadable_sample_but_preserves_counts():
    report = _operator_reap_report(execute=False)
    report["unreadable"] = [f"{TEST_UNREADABLE_PROCESS_PID + offset}: RuntimeError"
                            for offset in range(cli.REAP_UNREADABLE_SAMPLE_MAX + 3)]
    return_code, output, _ = _invoke_reap_orphans_cli(["--json"], report)
    payload = json.loads(output)
    assert return_code == 0
    assert len(payload["unreadable"]) == cli.REAP_UNREADABLE_SAMPLE_MAX
    assert payload["unreadable_count"] == cli.REAP_UNREADABLE_SAMPLE_MAX + 3
    assert payload["unreadable_truncated"] is True
    assert payload["cross_uid_skipped"] == TEST_CROSS_UID_SKIPPED


def test_reap_orphans_cli_exit_one_only_for_reap_errors():
    return_code, output, _ = _invoke_reap_orphans_cli(["--execute"], _operator_reap_report(execute=True, error=True))
    assert return_code == 1
    assert TEST_REAP_ERROR in output


def test_periodic_fleet_registry_contains_orphan_reaper_job():
    jobs = fleet.periodic_launchd_job_registry()
    matches = [job for job in jobs if job["Label"] == fleet.ORPHAN_REAP_LAUNCHD_LABEL]
    assert len(matches) == 1
    job = matches[0]
    assert job["StartInterval"] == fleet.ORPHAN_REAP_LAUNCHD_INTERVAL_S
    assert job["ProgramArguments"][1:1 + len(fleet.ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS)] == list(
        fleet.ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS)
    assert fleet.ORPHAN_REAP_BOUNDED_LOG_FLAG in job["ProgramArguments"]
    assert str(fleet.config.HOME / fleet.ORPHAN_REAP_LOG_BASENAME) in job["ProgramArguments"]
    assert job["StandardOutPath"] == job["StandardErrorPath"]
    assert job["StandardOutPath"] == os.devnull


def test_periodic_fleet_registry_contains_lane_leak_tripwire_job():
    jobs = fleet.periodic_launchd_job_registry()
    matches = [job for job in jobs if job["Label"] == fleet.LANE_LEAK_TRIPWIRE_LAUNCHD_LABEL]
    assert len(matches) == 1
    job = matches[0]
    assert job["StartInterval"] == fleet.LANE_LEAK_TRIPWIRE_LAUNCHD_INTERVAL_S
    assert job["ProgramArguments"][0] == str(fleet.pathlib.Path(fleet.sys.executable).with_name("spendguard"))
    assert job["ProgramArguments"][1:1 + len(fleet.LANE_LEAK_TRIPWIRE_LAUNCHD_COMMAND_ARGUMENTS)] == list(
        fleet.LANE_LEAK_TRIPWIRE_LAUNCHD_COMMAND_ARGUMENTS)
    assert fleet.LANE_LEAK_TRIPWIRE_BOUNDED_LOG_FLAG in job["ProgramArguments"]
    assert str(fleet.config.HOME / fleet.LANE_LEAK_TRIPWIRE_LOG_BASENAME) in job["ProgramArguments"]
    install_name = job["Label"] + fleet.FLEET_LAUNCHD_PLIST_SUFFIX
    assert install_name == fleet.LANE_LEAK_TRIPWIRE_LAUNCHD_LABEL + fleet.FLEET_LAUNCHD_PLIST_SUFFIX
    assert job["RunAtLoad"] is True
    assert job["StandardOutPath"] == job["StandardErrorPath"] == os.devnull


def test_bounded_fleet_log_rotates_one_generation_and_caps_live_file():
    with tempfile.TemporaryDirectory() as directory:
        log_path = os.path.join(directory, fleet.ORPHAN_REAP_LOG_BASENAME)
        record = "x" * 12
        cap = 20
        fleet.append_bounded_fleet_log_record(log_path, record, max_bytes=cap, keep=fleet.FLEET_LOG_KEEP)
        fleet.append_bounded_fleet_log_record(log_path, record, max_bytes=cap, keep=fleet.FLEET_LOG_KEEP)
        assert os.path.getsize(log_path) <= cap
        assert os.path.exists(f"{log_path}.1")
        assert not os.path.exists(f"{log_path}.{fleet.FLEET_LOG_KEEP + 1}")


if __name__ == "__main__":
    cases = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for case in cases:
        case()
        print(f"  [OK] {case.__name__}")
    print(f"\nOK test_reap_orphans_cli_and_fleet: {len(cases)} test(s) passed")
