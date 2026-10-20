"""Parser-fact reuse preserves write ownership and bounded read cancellation."""
import pathlib
import sqlite3
import sys
import threading
import time

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_read, ledger_request_cursor
from spendguard.ledger_read import LedgerQueryDeadline, LedgerRequestAbandoned, ledger_request_scope

PARSER_CACHE_CREATE_SQL = "CREATE TABLE parser_cache_guard (value INTEGER)"
PARSER_CACHE_INSERT_SQL = "INSERT INTO parser_cache_guard VALUES (?)"
PARSER_CACHE_RETURNING_SQL = PARSER_CACHE_INSERT_SQL + " RETURNING value"
PARSER_CACHE_SELECT_SQL = "SELECT value FROM parser_cache_guard"
PARSER_CACHE_COUNT_SQL = "SELECT COUNT(*) FROM parser_cache_guard"
PARSER_CACHE_TRIGGER_SQL = """CREATE TRIGGER parser_cache_trigger AFTER INSERT
ON parser_cache_guard BEGIN INSERT INTO parser_cache_audit VALUES (NEW.value); END"""
PARSER_CACHE_AUDIT_CREATE_SQL = "CREATE TABLE parser_cache_audit (value INTEGER)"
PARSER_CACHE_AUDIT_COUNT_SQL = "SELECT COUNT(*) FROM parser_cache_audit"
PARSER_CACHE_SLOW_SQL = """WITH RECURSIVE parser_counter(value) AS
(VALUES(0) UNION ALL SELECT value+1 FROM parser_counter) SELECT SUM(value) FROM parser_counter"""
PARSER_CACHE_DEADLINE_S = 0.02
PARSER_CACHE_WAIT_S = 2.0
PARSER_CACHE_FIRST_VALUE = 1
PARSER_CACHE_SECOND_VALUE = 2
PARSER_CACHE_SINGLE_ENTRY = 1
PARSER_CACHE_NO_ROWS = 0
PARSER_CACHE_TWO_ROWS = 2
PARSER_CACHE_DELAY_MULTIPLIER = 2
PARSER_CACHE_DELAY_FUNCTION_NAME = "managed_parser_cache_delay"
PARSER_CACHE_DELAY_SQL = f"SELECT {PARSER_CACHE_DELAY_FUNCTION_NAME}()"
PARSER_CACHE_UPDATE_SQL = "UPDATE parser_cache_guard SET value=value+1"
PARSER_CACHE_FINITE_SCAN_ROWS = 5_000_000
PARSER_CACHE_FINITE_SCAN_SQL = f"""WITH RECURSIVE parser_nested_counter(value) AS
(VALUES(0) UNION ALL SELECT value+1 FROM parser_nested_counter
WHERE value < {PARSER_CACHE_FINITE_SCAN_ROWS}) SELECT SUM(value) FROM parser_nested_counter"""


def test_managed_parser_cache_reuses_compilation_and_preserves_writes(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    authorizations = []

    def observe_managed_parser_cache_actions(action, first, second, database, source):
        if action == sqlite3.SQLITE_INSERT:
            authorizations.append((action, first, second, database, source))
        return sqlite3.SQLITE_OK

    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        connection.set_authorizer(observe_managed_parser_cache_actions)
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.commit()
        first_compilation = tuple(authorizations)
        assert first_compilation
        with ledger_request_scope() as abandoned_scope:
            abandoned_scope.cancel_ledger_request()
            cursor = connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_SECOND_VALUE,))
        assert tuple(authorizations) == first_compilation, "identical SQL must retain SQLite's compiled statement"
        assert cursor._ledger_request_cursor_state.writes
        assert connection._ledger_transaction_has_writes
        connection.commit()
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
    finally:
        connection.close()


def test_managed_parser_cache_eviction_reparses_cached_write(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_request_cursor, "MANAGED_STATEMENT_FACT_CACHE_CAPACITY", PARSER_CACHE_SINGLE_ENTRY)
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.execute(PARSER_CACHE_SELECT_SQL).fetchall()
        cursor = connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_SECOND_VALUE,))
        assert cursor._ledger_request_cursor_state.writes, "evicted facts cannot make SQLite's cached INSERT a read"
        assert connection._ledger_transaction_has_writes
        connection.commit()
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
    finally:
        connection.close()


def test_managed_parser_cache_schema_reprepare_and_authorizer_denial(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        connection.execute(PARSER_CACHE_AUDIT_CREATE_SQL)
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.commit()
        connection.execute(PARSER_CACHE_TRIGGER_SQL)
        cursor = connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_SECOND_VALUE,))
        assert cursor._ledger_request_cursor_state.writes
        connection.commit()
        assert connection.execute(PARSER_CACHE_AUDIT_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_SINGLE_ENTRY

        def deny_managed_parser_cache_writes(action, first, second, database, source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK

        connection.set_authorizer(deny_managed_parser_cache_writes)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.set_authorizer(None)
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
    finally:
        connection.close()


def test_managed_parser_cache_returning_writes_and_repeated_read_deadlines(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        with ledger_request_scope():
            for value in (PARSER_CACHE_FIRST_VALUE, PARSER_CACHE_SECOND_VALUE):
                cursor = connection.execute(PARSER_CACHE_RETURNING_SQL, (value,))
                assert cursor.fetchone() == (value,)
                assert cursor._ledger_request_cursor_state.writes
                cursor.close()
            connection.commit()
        for _ in (PARSER_CACHE_FIRST_VALUE, PARSER_CACHE_SECOND_VALUE):
            with pytest.raises(LedgerQueryDeadline):
                with ledger_request_scope(deadline_s=PARSER_CACHE_DEADLINE_S):
                    connection.execute(PARSER_CACHE_SLOW_SQL).fetchone()
        connection.execute(PARSER_CACHE_SELECT_SQL).fetchall()
        with ledger_request_scope() as abandoned_scope:
            abandoned_scope.cancel_ledger_request()
            with pytest.raises(LedgerRequestAbandoned):
                connection.execute(PARSER_CACHE_SELECT_SQL).fetchall()
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
    finally:
        connection.close()


def test_managed_parser_cache_concurrent_cursor_owners_keep_parser_facts(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    errors = []
    ready = threading.Barrier(PARSER_CACHE_TWO_ROWS)

    def run_managed_parser_cache_concurrent_writer(value):
        try:
            ready.wait(timeout=PARSER_CACHE_WAIT_S)
            with ledger_request_scope():
                cursor = connection.execute(PARSER_CACHE_INSERT_SQL, (value,))
                assert cursor._ledger_request_cursor_state.writes
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=run_managed_parser_cache_concurrent_writer, args=(value,))
               for value in (PARSER_CACHE_FIRST_VALUE, PARSER_CACHE_SECOND_VALUE)]
    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(PARSER_CACHE_WAIT_S)
            assert not worker.is_alive()
        assert not errors, errors
        connection.commit()
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
    finally:
        for worker in workers:
            worker.join(PARSER_CACHE_WAIT_S)
        connection.close()


def test_managed_parser_cache_allocates_default_scope_only_for_reads(tmp_path, monkeypatch):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    scope_allocations = []
    scope_type = ledger_read.LedgerRequestReadScope

    def observe_managed_parser_cache_default_scope(deadline_s):
        scope_allocations.append(deadline_s)
        return scope_type(deadline_s)

    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        monkeypatch.setattr(ledger_read, "LedgerRequestReadScope", observe_managed_parser_cache_default_scope)
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.commit()
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_SECOND_VALUE,))
        connection.commit()
        assert not scope_allocations, "parser-proven writes must not allocate unused read cancellation owners"
        assert connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()[PARSER_CACHE_NO_ROWS] == PARSER_CACHE_TWO_ROWS
        assert scope_allocations, "default reads must retain a real cancellation owner"
    finally:
        connection.close()


def test_managed_parser_cache_lazy_scope_keeps_pre_step_deadline(tmp_path, monkeypatch):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)

    def pause_managed_parser_cache_sql_step():
        time.sleep(PARSER_CACHE_DEADLINE_S * PARSER_CACHE_DELAY_MULTIPLIER)
        return PARSER_CACHE_FIRST_VALUE

    try:
        connection.create_function(PARSER_CACHE_DELAY_FUNCTION_NAME, PARSER_CACHE_NO_ROWS,
                                   pause_managed_parser_cache_sql_step)
        monkeypatch.setattr(config, "LEDGER_QUERY_DEADLINE_S", PARSER_CACHE_DEADLINE_S)
        with pytest.raises(LedgerQueryDeadline):
            connection.execute(PARSER_CACHE_DELAY_SQL).fetchone()
    finally:
        connection.close()


def test_managed_parser_cache_nested_authorizer_preserves_outer_write(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    nested_reads = []

    def read_inside_managed_parser_cache_update_authorizer(action, first, second, database, source):
        if action == sqlite3.SQLITE_READ and not nested_reads:
            nested_reads.append(True)
            connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()
        return sqlite3.SQLITE_OK

    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        connection.execute(PARSER_CACHE_INSERT_SQL, (PARSER_CACHE_FIRST_VALUE,))
        connection.commit()
        connection.set_authorizer(read_inside_managed_parser_cache_update_authorizer)
        cursor = connection.execute(PARSER_CACHE_UPDATE_SQL)
        assert nested_reads
        assert cursor._ledger_request_cursor_state.writes
        assert connection._ledger_transaction_has_writes
        connection.set_authorizer(None)
        connection.commit()
        assert connection.execute(PARSER_CACHE_SELECT_SQL).fetchone() == (PARSER_CACHE_SECOND_VALUE,)
    finally:
        connection.close()


def test_managed_parser_cache_nested_authorizer_restores_outer_progress(tmp_path):
    connection = config.open_ledger_connection(tmp_path / pathlib.Path(__file__).stem)
    nested_reads = []

    def read_inside_managed_parser_cache_scan_authorizer(action, first, second, database, source):
        if action == sqlite3.SQLITE_SELECT and not nested_reads:
            nested_reads.append(True)
            connection.execute(PARSER_CACHE_COUNT_SQL).fetchone()
        return sqlite3.SQLITE_OK

    try:
        connection.execute(PARSER_CACHE_CREATE_SQL)
        connection.set_authorizer(read_inside_managed_parser_cache_scan_authorizer)
        cursor = connection.cursor()
        with pytest.raises(LedgerQueryDeadline):
            with ledger_request_scope(deadline_s=PARSER_CACHE_DEADLINE_S):
                cursor.execute(PARSER_CACHE_FINITE_SCAN_SQL)
        assert nested_reads
        assert isinstance(cursor._ledger_request_cursor_state.abort_error, LedgerQueryDeadline), (
            "outer VM progress must interrupt the scan, rather than detecting expiry only after completion")
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
