"""Writer failures resolve callers; bulk reservations preserve commit boundaries."""
import concurrent.futures
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

from spendguard import config
from spendguard.ledger_writer import (
    close_ledger_writers, current_ledger_writer_connection,
    ledger_writer_bulk_transaction, run_ledger_write,
)

WRITER_FAILURE_TEST_WAIT_SECONDS = 10
WRITER_FAILURE_TEST_MESSAGE = "injected writer startup failure"
WRITER_FAILURE_TEST_TABLE = "CREATE TABLE writer_failure_probe(value INTEGER)"


def run_writer_failure_boundary_probe():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "writer.sqlite"
        with patch.object(config, "open_ledger_connection", side_effect=OSError(WRITER_FAILURE_TEST_MESSAGE)):
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = executor.submit(run_ledger_write, lambda: None, path)
                try:
                    result.result(timeout=WRITER_FAILURE_TEST_WAIT_SECONDS)
                except OSError as error:
                    assert str(error) == WRITER_FAILURE_TEST_MESSAGE
                else:
                    raise AssertionError("writer startup failure was swallowed")
        run_ledger_write(lambda: current_ledger_writer_connection().execute(WRITER_FAILURE_TEST_TABLE), path)
        def raise_writer_callback_timeout():
            raise TimeoutError(WRITER_FAILURE_TEST_MESSAGE)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            callback_failure = executor.submit(run_ledger_write, raise_writer_callback_timeout, path)
            try:
                callback_failure.result(timeout=WRITER_FAILURE_TEST_WAIT_SECONDS)
            except TimeoutError as error:
                assert callback_failure.done(), "callback timeout was mistaken for an unresolved future"
                assert str(error) == WRITER_FAILURE_TEST_MESSAGE
            else:
                raise AssertionError("writer callback timeout was swallowed")
        try:
            with ledger_writer_bulk_transaction(path):
                run_ledger_write(lambda: current_ledger_writer_connection().execute(
                    "INSERT INTO writer_failure_probe VALUES (?)", (1,)), path)
                raise RuntimeError(WRITER_FAILURE_TEST_MESSAGE)
        except RuntimeError:
            pass
        assert run_ledger_write(lambda: current_ledger_writer_connection().execute(
            "SELECT COUNT(*) FROM writer_failure_probe").fetchone()[0], path) == 0
        entered = threading.Event()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            with ledger_writer_bulk_transaction(path):
                def probe_reserved_writer_order():
                    entered.set()
                    return run_ledger_write(lambda: current_ledger_writer_connection().execute(
                        "SELECT COUNT(*) FROM writer_failure_probe").fetchone()[0], path)
                waiting = executor.submit(probe_reserved_writer_order)
                assert entered.wait(WRITER_FAILURE_TEST_WAIT_SECONDS)
                assert not waiting.done()
                run_ledger_write(lambda: current_ledger_writer_connection().execute(
                    "INSERT INTO writer_failure_probe VALUES (?)", (1,)), path)
            assert waiting.result(timeout=WRITER_FAILURE_TEST_WAIT_SECONDS) == 1
        close_ledger_writers()
    print("PASS: startup failure propagates; bulk rollback and concurrent admission preserve boundaries")


if __name__ == "__main__":
    run_writer_failure_boundary_probe()
