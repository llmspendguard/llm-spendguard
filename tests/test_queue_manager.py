"""Offline contract tests for structural queue cleanup and bounded conditional processing."""
import datetime
import json
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-queue-manager-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import cli, fleet, lane_queue

TEST_INTENT = "queue-manager-contract"
TEST_STALE_DAYS = 1.0
TEST_RETAIN_DAYS = 7.0
TEST_OLD_DAYS = 30.0
TEST_FUTURE_DAYS = 1.0
TEST_ARCHIVE_FILENAME = "archive.jsonl"
TEST_CORRUPT_ARCHIVE_LINE = "{not-json"
TEST_UNDECODABLE_LINE_COUNT = 1


def _queue_manager_timestamp(days):
    return lane_queue._iso(lane_queue._utcnow() + datetime.timedelta(days=days))


def _insert_queue_manager_row(state, created_ts, deadline_ts=None, lease_until=None, attempts=0, max_attempts=3):
    with lane_queue._queue_op() as queue_connection:
        cursor = queue_connection.execute(
            "INSERT INTO lane_queue(intent,task,state,attempts,max_attempts,deadline_ts,lease_until,worker,"
            "created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (TEST_INTENT, state, state, attempts, max_attempts, deadline_ts, lease_until, "test-worker",
             created_ts, created_ts))
        return cursor.lastrowid


def _queue_manager_state(row_id):
    with lane_queue._queue_op() as queue_connection:
        row = queue_connection.execute("SELECT state,worker FROM lane_queue WHERE id=?", (row_id,)).fetchone()
    return row


def test_reclaim_expired_leases_and_claim_share_the_rule(monkeypatch):
    old = _queue_manager_timestamp(-TEST_OLD_DAYS)
    future = _queue_manager_timestamp(TEST_FUTURE_DAYS)
    pending_id = _insert_queue_manager_row("leased", old, lease_until=old, attempts=1, max_attempts=3)
    failed_id = _insert_queue_manager_row("leased", old, lease_until=old, attempts=3, max_attempts=3)
    live_id = _insert_queue_manager_row("leased", old, lease_until=future, attempts=1, max_attempts=3)
    result = lane_queue.reclaim_expired_leases()
    assert result == {"reclaimed_to_pending": 1, "failed_exhausted": 1}
    assert _queue_manager_state(pending_id) == ("pending", None)
    assert _queue_manager_state(failed_id)[0] == "failed"
    assert _queue_manager_state(live_id) == ("leased", "test-worker")
    _insert_queue_manager_row("leased", old, lease_until=old, attempts=1, max_attempts=3)
    original_reclaim = lane_queue.reclaim_expired_leases
    claim_connections = []

    def recorded_claim_reclaim(connection=None, now=None):
        claim_connections.append(connection)
        return original_reclaim(connection=connection, now=now)

    monkeypatch.setattr(lane_queue, "reclaim_expired_leases", recorded_claim_reclaim)
    lane_queue.lease(1)
    assert any(connection is not None for connection in claim_connections)


def test_stale_retirement_is_archive_first_and_excludes_fresh_terminal_and_live_lease(tmp_path):
    old = _queue_manager_timestamp(-TEST_OLD_DAYS)
    now = _queue_manager_timestamp(0)
    future = _queue_manager_timestamp(TEST_FUTURE_DAYS)
    deadline_id = _insert_queue_manager_row("pending", now, deadline_ts=old)
    age_id = _insert_queue_manager_row("pending", old)
    fresh_id = _insert_queue_manager_row("pending", now, deadline_ts=future)
    done_id = _insert_queue_manager_row("done", now)
    failed_id = _insert_queue_manager_row("failed", now)
    live_lease_id = _insert_queue_manager_row("leased", old, lease_until=future)
    archive = tmp_path / "queue-archive.jsonl"
    result = lane_queue.purge(retain_days=TEST_RETAIN_DAYS, stale_days=TEST_STALE_DAYS,
                              archive_path=str(archive), chunk=1)
    assert result["stale"] == {"archived": 2, "retired": 2, "deleted": 0}
    archived = [json.loads(line) for line in archive.read_text().splitlines()]
    assert {record["id"] for record in archived} == {deadline_id, age_id}
    assert lane_queue.queue_row_status(deadline_id, str(archive))["disposition"] == "retired"
    assert lane_queue.queue_row_status(deadline_id, str(archive))["reason"] == "stale_deadline"
    assert lane_queue.queue_row_status(age_id, str(archive))["reason"] == "stale_age"
    assert _queue_manager_state(fresh_id)[0] == "pending"
    assert _queue_manager_state(done_id)[0] == "done"
    assert _queue_manager_state(failed_id)[0] == "failed"
    assert _queue_manager_state(live_lease_id)[0] == "leased"
    with lane_queue._queue_op() as queue_connection:
        queue_connection.execute("UPDATE lane_queue SET updated_ts=? WHERE id IN (?,?)", (old, deadline_id, age_id))
    terminal = lane_queue.purge(retain_days=TEST_RETAIN_DAYS, stale_days=TEST_STALE_DAYS,
                                archive_path=str(archive), chunk=1)
    assert terminal["terminal"] == {"archived": 2, "deleted": 2}
    assert lane_queue.queue_row_status(deadline_id, str(archive))["disposition"] == "retired"
    assert lane_queue.queue_row_status(deadline_id, str(archive))["source"] == "archive"


def test_queue_row_status_all_dispositions_and_unknown(tmp_path):
    now = _queue_manager_timestamp(0)
    statuses = {}
    for state in ("pending", "leased", "done", "failed", "retired"):
        row_id = _insert_queue_manager_row(state, now)
        if state == "retired":
            with lane_queue._queue_op() as queue_connection:
                queue_connection.execute("UPDATE lane_queue SET terminal_reason=? WHERE id=?", ("stale_age", row_id))
        statuses[state] = lane_queue.queue_row_status(row_id, str(tmp_path / "archive.jsonl"))
    assert statuses["pending"]["disposition"] == "queued"
    assert {state: statuses[state]["disposition"] for state in ("leased", "done", "failed", "retired")} == {
        state: state for state in ("leased", "done", "failed", "retired")}
    assert statuses["retired"]["reason"] == "stale_age"
    assert lane_queue.queue_row_status(max(item["id"] for item in statuses.values()) + 1,
                                       str(tmp_path / TEST_ARCHIVE_FILENAME))["disposition"] == "not_found"


def test_queue_row_status_distinguishes_unreadable_archive_from_clean_absence(tmp_path, capsys):
    now = _queue_manager_timestamp(0)
    corrupt_absent_id = _insert_queue_manager_row("done", now)
    corrupt_found_id = _insert_queue_manager_row("done", now)
    clean_absent_id = _insert_queue_manager_row("done", now)
    with lane_queue._queue_op() as queue_connection:
        queue_connection.execute("DELETE FROM lane_queue WHERE id IN (?,?,?)",
                                 (corrupt_absent_id, corrupt_found_id, clean_absent_id))
    archive = tmp_path / TEST_ARCHIVE_FILENAME

    archive.write_text(TEST_CORRUPT_ARCHIVE_LINE + "\n", encoding="utf-8")
    unreadable_status = lane_queue.queue_row_status(corrupt_absent_id, str(archive))
    assert unreadable_status == {"id": corrupt_absent_id, "disposition": "indeterminate",
                                 "reason": "archive_unreadable", "undecodable": TEST_UNDECODABLE_LINE_COUNT,
                                 "archive": str(archive)}
    assert "lane_queue.queue_row_status" in capsys.readouterr().err

    archive.write_text(TEST_CORRUPT_ARCHIVE_LINE + "\n" +
                       json.dumps({"id": corrupt_found_id, "state": "done"}) + "\n", encoding="utf-8")
    assert lane_queue.queue_row_status(corrupt_found_id, str(archive))["disposition"] == "done"

    archive.write_text(json.dumps({"id": corrupt_found_id, "state": "done"}) + "\n", encoding="utf-8")
    assert lane_queue.queue_row_status(clean_absent_id, str(archive))["disposition"] == "not_found"


def test_manager_bounded_processing_gates(monkeypatch):
    calls = []

    def recorded_queue_manager_drain(**kwargs):
        calls.append(kwargs)
        return {"ran": 0}

    monkeypatch.setattr(lane_queue, "drain", recorded_queue_manager_drain)
    monkeypatch.setattr(lane_queue, "reclaim_expired_leases",
                        lambda: {"reclaimed_to_pending": 0, "failed_exhausted": 0})
    monkeypatch.setattr(lane_queue, "purge", lambda **kwargs: {"archived": 0, "deleted": 0})
    monkeypatch.setattr(lane_queue, "queue_management_plan",
                        lambda stale_days=None, retain_days=None: {"ready_pending": 0, "expired_leases": [],
                                                                  "stale_live": [], "terminal_retention": []})
    monkeypatch.setattr(lane_queue, "queue_depth", lambda: {"pending": 0, "parked": 0})
    assert lane_queue.manage_queue_tick(execute=True)["process"]["invoked"] is False
    monkeypatch.setattr(lane_queue, "queue_management_plan",
                        lambda stale_days=None, retain_days=None: {"ready_pending": 1, "expired_leases": [],
                                                                  "stale_live": [], "terminal_retention": []})
    monkeypatch.setattr(lane_queue, "_overloaded", lambda ceiling: False)
    monkeypatch.setattr(lane_queue, "queue_depth", lambda: {"pending": 1, "parked": 0})
    report = lane_queue.manage_queue_tick(execute=True)
    assert report["process"]["invoked"] is True
    assert len(calls) == 1 and calls[0]["forever"] is False
    lane_queue.manage_queue_tick(execute=True, process=False)
    assert len(calls) == 1
    monkeypatch.setattr(lane_queue, "_overloaded", lambda ceiling: True)
    lane_queue.manage_queue_tick(execute=True)
    assert len(calls) == 1


def test_queue_manager_cli_exit_contract(monkeypatch, capsys):
    monkeypatch.setattr(lane_queue, "manage_queue_tick",
                        lambda **kwargs: {"mode": "dry-run", "plan": {}, "process": {}})
    assert cli.manage_lane_queue_cli(["--json"]) == 0
    json.loads(capsys.readouterr().out)
    monkeypatch.setattr(lane_queue, "manage_queue_tick",
                        lambda **kwargs: {"mode": "dry-run", "plan": {}, "process": {}, "error": "real failure"})
    assert cli.manage_lane_queue_cli(["--json"]) == 1


def test_periodic_registry_contains_both_bounded_log_jobs():
    jobs = fleet.periodic_launchd_job_registry()
    assert {job["Label"] for job in jobs} == {fleet.ORPHAN_REAP_LAUNCHD_LABEL, fleet.QUEUE_MANAGE_LAUNCHD_LABEL}
    queue_job = next(job for job in jobs if job["Label"] == fleet.QUEUE_MANAGE_LAUNCHD_LABEL)
    assert queue_job["StartInterval"] == fleet.QUEUE_MANAGE_LAUNCHD_INTERVAL_S
    assert queue_job["ProgramArguments"][1:1 + len(fleet.QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS)] == list(
        fleet.QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS)
    assert fleet.QUEUE_MANAGE_BOUNDED_LOG_FLAG in queue_job["ProgramArguments"]
    assert str(fleet.config.HOME / fleet.QUEUE_MANAGE_LOG_BASENAME) in queue_job["ProgramArguments"]
    assert queue_job["StandardOutPath"] == queue_job["StandardErrorPath"] == os.devnull


def test_first_queue_manager_install_cleans_without_processing_and_reinstall_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.delenv(fleet.FLEET_TEST_ISOLATION_ENV, raising=False)
    monkeypatch.setattr(fleet.sys, "platform", "darwin")
    monkeypatch.setattr(fleet.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(fleet.config, "HOME", tmp_path / "spendguard-home")
    clean_calls = []
    monkeypatch.setattr(lane_queue, "manage_queue_tick",
                        lambda **kwargs: clean_calls.append(kwargs) or {"mode": "execute", "process": {}})
    monkeypatch.setattr(fleet.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 0})())
    fleet.install_periodic_launchd_fleet_jobs()
    fleet.install_periodic_launchd_fleet_jobs()
    assert clean_calls == [{"execute": True, "process": False}]


def test_fleet_install_guards_skip_clean_pass(monkeypatch):
    clean_calls = []
    monkeypatch.setattr(lane_queue, "manage_queue_tick", lambda **kwargs: clean_calls.append(kwargs))
    monkeypatch.setattr(fleet.sys, "platform", "linux")
    assert fleet.install_periodic_launchd_fleet_jobs() == []
    monkeypatch.setattr(fleet.sys, "platform", "darwin")
    monkeypatch.setenv(fleet.FLEET_TEST_ISOLATION_ENV, "1")
    assert fleet.install_periodic_launchd_fleet_jobs() == []
    assert clean_calls == []
