"""Offline reproduction of bounded delegate-job writer contention and diagnostics."""
import os
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style
from _runner_budget_calibration import effective_runner_budget_scale

align_test_startup_home("sg-job-contention-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
from spendguard import delegate_jobs  # noqa: E402
from spendguard.execution_journal import DurableExecutionPersistenceError  # noqa: E402

CONTENTION_TEST_TIMEOUT_MS = 40
CONTENTION_TEST_RELEASE_S = 0.1
CONTENTION_TEST_PATIENT_TIMEOUT_MS = 1000
CONTENTION_TEST_MAX_ELAPSED_S = 2
CONTENTION_TEST_FAILURE_MESSAGE = "injected delegate writer refusal"
CONTENTION_TEST_TABLE_NAME = "delegate_contention_probe"


def test_delegate_writer_failure_preserves_exception_and_database(tmp_path):
    database_path = tmp_path / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    with patch.object(delegate_jobs, "delegate_jobs_database_path", return_value=database_path), patch.object(
            delegate_jobs.config, "fresh_ledger_conn", side_effect=sqlite3.OperationalError(
                CONTENTION_TEST_FAILURE_MESSAGE)):
        try:
            with delegate_jobs._open_delegate_jobs_connection():
                raise AssertionError("failed writer unexpectedly yielded")
        except DurableExecutionPersistenceError as error:
            assert "OperationalError" in str(error), str(error)
            assert CONTENTION_TEST_FAILURE_MESSAGE in str(error), str(error)
            assert os.fspath(database_path) in str(error), str(error)
        else:
            raise AssertionError("persistence failure was swallowed")



def test_delegate_finish_failure_identifies_job(tmp_path):
    database_path = tmp_path / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    failure_job_id = tmp_path.name
    with patch.object(delegate_jobs, "delegate_jobs_database_path", return_value=database_path), patch.object(
            delegate_jobs, "_delegate_job_row", return_value={"provider": None, "execution_id": None}), patch.object(
            delegate_jobs.config, "fresh_ledger_conn", side_effect=sqlite3.OperationalError(
                CONTENTION_TEST_FAILURE_MESSAGE)):
        try:
            delegate_jobs.finish_delegate_job(failure_job_id, state=delegate_jobs.DELEGATE_JOB_DONE_STATE)
        except DurableExecutionPersistenceError as error:
            assert failure_job_id in str(error), str(error)
            assert CONTENTION_TEST_FAILURE_MESSAGE in str(error), str(error)
        else:
            raise AssertionError("failed finish unexpectedly succeeded")


def _delegate_contention_test_settings(section, key, default=None):
    if (section, key) == ("delegate_jobs", "busy_timeout_ms"):
        return CONTENTION_TEST_TIMEOUT_MS
    return default


def test_delegate_writer_contention_exhaustion_is_bounded_and_honest(tmp_path):
    database_path = tmp_path / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    with patch.object(delegate_jobs, "delegate_jobs_database_path", return_value=database_path):
        with delegate_jobs._open_delegate_jobs_connection() as setup:
            setup.execute(f"CREATE TABLE {CONTENTION_TEST_TABLE_NAME}(value TEXT)")
        with sqlite3.connect(database_path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with patch.object(delegate_jobs.config, "_cfg_get", side_effect=_delegate_contention_test_settings):
                started = time.monotonic()
                try:
                    with delegate_jobs._open_delegate_jobs_connection() as writer:
                        assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == CONTENTION_TEST_TIMEOUT_MS
                        writer.execute(f"INSERT INTO {CONTENTION_TEST_TABLE_NAME} VALUES(?)", ("probe",))
                except DurableExecutionPersistenceError as error:
                    elapsed = time.monotonic() - started
                    assert elapsed < CONTENTION_TEST_MAX_ELAPSED_S * effective_runner_budget_scale(), elapsed
                    assert "OperationalError" in str(error), str(error)
                    assert "database is locked" in str(error), str(error)
                    assert os.fspath(database_path) in str(error), str(error)
                else:
                    raise AssertionError("contended writer unexpectedly acquired lock")
            blocker.rollback()


def test_delegate_writer_contention_waits_until_lock_release(tmp_path):
    database_path = tmp_path / delegate_jobs.DELEGATE_JOBS_DATABASE_FILENAME
    with patch.object(delegate_jobs, "delegate_jobs_database_path", return_value=database_path):
        with delegate_jobs._open_delegate_jobs_connection() as setup:
            setup.execute(f"CREATE TABLE {CONTENTION_TEST_TABLE_NAME}(value TEXT)")
        blocker = sqlite3.connect(database_path, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(CONTENTION_TEST_RELEASE_S, blocker.rollback)
        release.start()
        original_cfg_get = delegate_jobs.config._cfg_get

        def contention_patient_settings(section, key, default=None):
            if (section, key) == ("delegate_jobs", "busy_timeout_ms"):
                return CONTENTION_TEST_PATIENT_TIMEOUT_MS
            return original_cfg_get(section, key, default)

        try:
            with patch.object(delegate_jobs.config, "_cfg_get", side_effect=contention_patient_settings):
                with delegate_jobs._open_delegate_jobs_connection() as writer:
                    assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == CONTENTION_TEST_PATIENT_TIMEOUT_MS
                    writer.execute(f"INSERT INTO {CONTENTION_TEST_TABLE_NAME} VALUES(?)", ("probe",))
            with sqlite3.connect(database_path) as reader:
                assert reader.execute(f"SELECT value FROM {CONTENTION_TEST_TABLE_NAME}").fetchall() == [("probe",)]
        finally:
            release.join()
            blocker.close()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
