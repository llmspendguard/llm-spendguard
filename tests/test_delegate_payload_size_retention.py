"""Lossless finish-time containment and configured terminal history retention."""
import datetime
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())
from spendguard.manage.token_operations import delegate_jobs as payload_jobs

PAYLOAD_LIMIT_BYTES = 1024
PAYLOAD_REPETITIONS = 32768
HISTORY_WINDOW_SECONDS = 3600
EXPIRED_AGE_MULTIPLIER = 2
PAYLOAD_TEST_JOB = "size-retention-job"


def test_delegate_payload_finish_containment_and_retention():
    with tempfile.TemporaryDirectory() as home:
        original_get = payload_jobs.config._cfg_get
        settings = {("delegate_jobs", "payload_inline_max_bytes"): PAYLOAD_LIMIT_BYTES,
                    ("delegate_jobs", "vacuum_min_freed_bytes"): 0,
                    ("delegate", "jobs_retention_s"): HISTORY_WINDOW_SECONDS}
        def payload_policy_config(section, key, default=None):
            return settings.get((section, key), original_get(section, key, default))
        with mock.patch.object(payload_jobs.config, "HOME", Path(home)), mock.patch.object(
                payload_jobs.config, "_cfg_get", side_effect=payload_policy_config):
            timestamp = payload_jobs._delegate_job_timestamp()
            with payload_jobs._open_delegate_jobs_connection() as connection:
                connection.execute(
                    "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,"
                    "cwd,dirty_before) VALUES(?,?,?,?,?,?,?,?)",
                    (PAYLOAD_TEST_JOB, timestamp, timestamp, payload_jobs.DELEGATE_JOB_RUNNING_STATE,
                     "agentic", PAYLOAD_TEST_JOB, home, "[]"))
            whole_payload = {"text": "whole evidence 🐘" * PAYLOAD_REPETITIONS, "tail": "final evidence"}
            payload_jobs.finish_delegate_job(PAYLOAD_TEST_JOB, state=payload_jobs.DELEGATE_JOB_DONE_STATE,
                                             result_payload=whole_payload)
            row = payload_jobs._delegate_job_row(PAYLOAD_TEST_JOB)
            assert len(row["result_payload"].encode()) < PAYLOAD_LIMIT_BYTES, "large body stayed inline"
            archive = Path(row["payload_archive_path"])
            assert archive.is_file()
            assert payload_jobs.read_delegate_job_result(PAYLOAD_TEST_JOB)["text"] == whole_payload["text"]
            assert payload_jobs.read_delegate_job_result(PAYLOAD_TEST_JOB)["tail"] == whole_payload["tail"]
            with payload_jobs._open_delegate_jobs_connection() as connection:
                allocated_bytes = (connection.execute("PRAGMA page_count").fetchone()[0]
                                   * connection.execute("PRAGMA page_size").fetchone()[0])
            assert allocated_bytes < len(json.dumps(whole_payload).encode()), "database retained full body pages"
            expired = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                seconds=HISTORY_WINDOW_SECONDS * EXPIRED_AGE_MULTIPLIER)).isoformat()
            with payload_jobs._open_delegate_jobs_connection() as connection:
                connection.execute("UPDATE delegate_jobs SET updated_ts=? WHERE job_id=?",
                                   (expired, PAYLOAD_TEST_JOB))
                connection.execute(
                    "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,"
                    "cwd,dirty_before,result_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    (home, timestamp, timestamp, payload_jobs.DELEGATE_JOB_DONE_STATE,
                     "agentic", home, home, "[]", json.dumps({"tail": home})))
            payload_jobs.prune_delegate_jobs()
            assert payload_jobs._delegate_job_row(PAYLOAD_TEST_JOB) is None, "expired history retained"
            assert not archive.exists(), "expired archive retained"
            assert payload_jobs._delegate_job_row(home) is not None, "young history pruned"


def test_delegate_configured_history_window():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as database_home, mock.patch.object(
            payload_jobs.config, "HOME", Path(database_home)):
        archive = Path(home) / (PAYLOAD_TEST_JOB + ".json.gz")
        archive.touch()
        expired = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=HISTORY_WINDOW_SECONDS * EXPIRED_AGE_MULTIPLIER)).isoformat()
        timestamp = payload_jobs._delegate_job_timestamp()
        with payload_jobs._open_delegate_jobs_connection() as connection:
            for identity, updated, archive_path in (
                    (PAYLOAD_TEST_JOB, expired, str(archive)), (home, timestamp, None)):
                connection.execute(
                    "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,"
                    "cwd,dirty_before,payload_archive_path) VALUES(?,?,?,?,?,?,?,?,?)",
                    (identity, updated, updated, payload_jobs.DELEGATE_JOB_DONE_STATE,
                     "agentic", identity, home, "[]", archive_path))
        payload_jobs.prune_delegate_jobs(retention_s=HISTORY_WINDOW_SECONDS)
        assert payload_jobs._delegate_job_row(PAYLOAD_TEST_JOB) is None, "expired row retained"
        assert not archive.exists(), "expired archive retained"
        assert payload_jobs._delegate_job_row(home) is not None


if __name__ == "__main__":
    test_delegate_payload_finish_containment_and_retention()
    test_delegate_configured_history_window()
