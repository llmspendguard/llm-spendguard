"""Offline cross-process writer admission with SQLite contention tolerance disabled."""
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import contextlib

MULTIPROCESS_WRITER_COUNT = 6
MULTIPROCESS_WRITES_PER_PROCESS = 20
MULTIPROCESS_TRANSACTION_HOLD_SECONDS = 0.01
MULTIPROCESS_WORKER_TIMEOUT_SECONDS = 60
MULTIPROCESS_TEST_TABLE = "multiprocess_writer_admission_proof"
MULTIPROCESS_DISABLE_ADMISSION_FLAG = "--without-os-admission"
MULTIPROCESS_OWNER_EDGE_FLAG = "--owner-edges"
MULTIPROCESS_FORK_HOLD_SECONDS = 0.05
MULTIPROCESS_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"


def exercise_cross_process_ledger_owner(ledger_path):
    sys.path.insert(0, os.fspath(MULTIPROCESS_SOURCE_ROOT))
    from spendguard.ledger_writer import current_ledger_writer_connection, run_ledger_write
    from spendguard import ledger_writer
    if MULTIPROCESS_DISABLE_ADMISSION_FLAG in sys.argv:
        ledger_writer._LedgerWriteOwner.cross_process_write_lease = lambda self: contextlib.nullcontext()

    def initialize_shared_writer_proof():
        connection = current_ledger_writer_connection()
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute(f"CREATE TABLE IF NOT EXISTS {MULTIPROCESS_TEST_TABLE} (worker INTEGER, ordinal INTEGER)")

    run_ledger_write(initialize_shared_writer_proof, ledger_path)
    for ordinal in range(MULTIPROCESS_WRITES_PER_PROCESS):
        def apply_shared_writer_proof(ordinal=ordinal):
            connection = current_ledger_writer_connection()
            connection.execute(f"INSERT INTO {MULTIPROCESS_TEST_TABLE} VALUES (?,?)", (os.getpid(), ordinal))
            time.sleep(MULTIPROCESS_TRANSACTION_HOLD_SECONDS)
        if ordinal % 3 == 0:
            with ledger_writer.ledger_writer_bulk_transaction(ledger_path):
                run_ledger_write(apply_shared_writer_proof, ledger_path)
        elif ordinal % 3 == 1:
            with ledger_writer.exclusive_ledger_writer_connection(ledger_path) as connection:
                apply_shared_writer_proof()
                connection.commit()
        else:
            run_ledger_write(apply_shared_writer_proof, ledger_path)


def verify_nested_and_forked_writer_ownership(ledger_path):
    sys.path.insert(0, os.fspath(MULTIPROCESS_SOURCE_ROOT))
    from spendguard import ledger_writer

    def apply_nested_bulk_callback():
        connection = ledger_writer.current_ledger_writer_connection()
        with ledger_writer.ledger_writer_bulk_transaction(ledger_path):
            connection.execute(f"INSERT INTO {MULTIPROCESS_TEST_TABLE} VALUES (?,?)", (os.getpid(), -1))
            with ledger_writer.ledger_writer_bulk_transaction(ledger_path):
                assert connection.in_transaction
        assert connection.in_transaction, "nested bulk committed its owner's transaction"

    ledger_writer.run_ledger_write(apply_nested_bulk_callback, ledger_path)
    with ledger_writer.ledger_writer_bulk_transaction(ledger_path):
        ledger_writer.run_ledger_write(apply_nested_bulk_callback, ledger_path)
        with ledger_writer.ledger_writer_bulk_transaction(ledger_path):
            ledger_writer.run_ledger_write(apply_nested_bulk_callback, ledger_path)
        assert ledger_writer.run_ledger_write(
            lambda: ledger_writer.current_ledger_writer_connection().in_transaction, ledger_path
        ), "nested caller bulk committed its outer transaction"
    with ledger_writer.exclusive_ledger_writer_connection(ledger_path) as connection:
        child_pid = os.fork()
        if child_pid == 0:
            try:
                assert ledger_writer.current_ledger_writer_connection() is None
                ledger_writer.run_ledger_write(apply_nested_bulk_callback, ledger_path)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        time.sleep(MULTIPROCESS_FORK_HOLD_SECONDS)
        assert os.waitpid(child_pid, os.WNOHANG) == (0, 0), "child bypassed the parent's active writer lease"
        connection.commit()
    deadline = time.monotonic() + MULTIPROCESS_WORKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid:
            assert os.waitstatus_to_exitcode(status) == 0, status
            return
        time.sleep(MULTIPROCESS_FORK_HOLD_SECONDS)
    raise AssertionError("forked writer retained the parent's lock descriptor")


def verify_cross_process_writer_admission(disable_admission=False):
    with tempfile.TemporaryDirectory() as fixture_home:
        ledger_path = pathlib.Path(fixture_home) / "writer-proof.db"
        environment = {**os.environ, "SPENDGUARD_HOME": fixture_home,
                       "SPENDGUARD_NO_AUTOINSTALL": "1", "SPENDGUARD_TEST_ISOLATED": "1"}
        worker_command = [sys.executable, "-S", __file__, os.fspath(ledger_path)]
        if disable_admission:
            worker_command.append(MULTIPROCESS_DISABLE_ADMISSION_FLAG)
        workers = [subprocess.Popen(
            worker_command,
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for _ in range(MULTIPROCESS_WRITER_COUNT)]
        failures = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=MULTIPROCESS_WORKER_TIMEOUT_SECONDS)
            if worker.returncode:
                failures.append((stdout, stderr))
        if disable_admission:
            assert failures and any("sqlite3.OperationalError: database is locked" in stderr
                                    for _, stderr in failures), failures
            print("pre-lock mutation RED: independent process owners raise database is locked")
            return
        assert not failures, failures
        import sqlite3
        with sqlite3.connect(ledger_path) as connection:
            recorded = connection.execute(f"SELECT COUNT(*), COUNT(DISTINCT worker) FROM {MULTIPROCESS_TEST_TABLE}").fetchone()
        assert recorded == (MULTIPROCESS_WRITER_COUNT * MULTIPROCESS_WRITES_PER_PROCESS,
                            MULTIPROCESS_WRITER_COUNT), recorded
        edges = subprocess.run([sys.executable, "-S", __file__, os.fspath(ledger_path),
                                MULTIPROCESS_OWNER_EDGE_FLAG], env=environment,
                               capture_output=True, text=True, timeout=MULTIPROCESS_WORKER_TIMEOUT_SECONDS)
        assert edges.returncode == 0, (edges.stdout, edges.stderr)
    print("cross-process writer admission: all writes committed with busy_timeout=0")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if MULTIPROCESS_OWNER_EDGE_FLAG in sys.argv:
            verify_nested_and_forked_writer_ownership(sys.argv[1])
        else:
            exercise_cross_process_ledger_owner(sys.argv[1])
    else:
        verify_cross_process_writer_admission(disable_admission=True)
        verify_cross_process_writer_admission()
