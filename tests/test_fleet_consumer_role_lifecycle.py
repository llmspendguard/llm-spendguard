"""Offline fleet consumer-role lifecycle coverage with a throwaway registry."""
import sqlite3
import os
import pathlib
import sys
import tempfile
import threading
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fleet-role-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, fleet, release


EPHEMERAL_PID = 92001
DAEMON_PID = 92002
RECORDED_CREATE_TIME = 1000.5
GREEN_SHA = "green-sha"
STALE_SHA = "stale-sha"
FIXTURE_COMMAND = "fixture consumer"
ROLE_COLUMN_NAME = "role"
ROLE_COLUMN_TYPE = "TEXT"
LEGACY_PID = 92003
DEFAULT_ROLE_PID = 92004
IDENTITY_SAFE_PID = 92005
FORK_PARENT_PID = 92006
FORK_CHILD_PID = 92007
TRANSIENT_RETRY_PID = 92008
MISMATCHED_CREATE_TIME = 2000.5
FORK_CHILD_CREATE_TIME = 3000.5
CONCURRENT_REGISTRATION_OBSERVATION_TIMEOUT_S = 1.0
CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S = config.LEDGER_WRITE_TIMEOUT_S
LEGACY_TABLE_SQL = (
    "CREATE TABLE consumers(pid INTEGER PRIMARY KEY, version TEXT, served_sha TEXT, served_short TEXT, "
    "dirty INTEGER, cmdline TEXT, launchd_label TEXT, registered_ts TEXT, venv_root TEXT, "
    "python_executable TEXT, pid_create_time TEXT)"
)


class _RoleFixtureProcess:
    def create_time(self):
        return RECORDED_CREATE_TIME


class _RegistrationTracingConnection:
    def __init__(self, connection, insert_statements):
        self._connection = connection
        self._insert_statements = insert_statements

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exception_details):
        return self._connection.__exit__(*exception_details)

    def close(self):
        return self._connection.close()

    def execute(self, statement, parameters=()):
        if statement.lstrip().upper().startswith("INSERT OR REPLACE INTO CONSUMERS"):
            self._insert_statements.append(statement)
        return self._connection.execute(statement, parameters)


class _FailingConsumerInsertConnection:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exception_details):
        return self._connection.__exit__(*exception_details)

    def close(self):
        return self._connection.close()

    def execute(self, statement, parameters=()):
        if statement.lstrip().upper().startswith("INSERT OR REPLACE INTO CONSUMERS"):
            raise sqlite3.OperationalError("injected transient consumer registration failure")
        return self._connection.execute(statement, parameters)


def test_ephemeral_stale_consumer_is_observable_without_restart_advice():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    fleet._fleet_db().close()
    with sqlite3.connect(str(registry_home / "fleet.db")) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(consumers)")}
        if ROLE_COLUMN_NAME not in columns:
            connection.execute(f"ALTER TABLE consumers ADD COLUMN {ROLE_COLUMN_NAME} {ROLE_COLUMN_TYPE}")
        connection.executemany(
            "INSERT INTO consumers(pid,served_sha,cmdline,pid_create_time,role) VALUES(?,?,?,?,?)",
            (
                (EPHEMERAL_PID, STALE_SHA, FIXTURE_COMMAND, repr(RECORDED_CREATE_TIME),
                 fleet.CONSUMER_ROLE_EPHEMERAL),
                (DAEMON_PID, STALE_SHA, FIXTURE_COMMAND, repr(RECORDED_CREATE_TIME), fleet.CONSUMER_ROLE_DAEMON),
            ),
        )

    with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
            patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
        restart_result = fleet.restart_stale()
        warned_pids = {warning["pid"] for warning in restart_result["warn"]}
        transient_pids = {
            consumer["pid"] for consumer in restart_result[fleet.FLEET_RESTART_TRANSIENT_KEY]
        }
    assert DAEMON_PID in warned_pids
    assert EPHEMERAL_PID not in warned_pids
    assert EPHEMERAL_PID in transient_pids
    assert DAEMON_PID not in transient_pids


def test_default_role_remains_warned_for_unsupervised_stale_consumer():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    with fleet._fleet_db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO consumers(pid,served_sha,cmdline,pid_create_time) VALUES(?,?,?,?)",
            (DEFAULT_ROLE_PID, STALE_SHA, FIXTURE_COMMAND, repr(RECORDED_CREATE_TIME)),
        )
    with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
            patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
        consumers = {consumer["pid"]: consumer for consumer in fleet.running_consumers()}
        warned_pids = {warning["pid"] for warning in fleet.restart_stale()["warn"]}
    assert consumers[DEFAULT_ROLE_PID][ROLE_COLUMN_NAME] == fleet.CONSUMER_ROLE_DAEMON
    assert DEFAULT_ROLE_PID in warned_pids


def test_consumer_deregistration_requires_matching_process_identity():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    recorded_create_time = repr(RECORDED_CREATE_TIME)
    with fleet._fleet_db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO consumers(pid,pid_create_time) VALUES(?,?)",
            (IDENTITY_SAFE_PID, recorded_create_time),
        )
    fleet.deregister_consumer(IDENTITY_SAFE_PID, repr(MISMATCHED_CREATE_TIME))
    with fleet._fleet_db() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM consumers WHERE pid=?", (IDENTITY_SAFE_PID,)
        ).fetchone()[0] == 1
    fleet.deregister_consumer(IDENTITY_SAFE_PID, recorded_create_time)
    with fleet._fleet_db() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM consumers WHERE pid=?", (IDENTITY_SAFE_PID,)
        ).fetchone()[0] == 0


def test_consumer_role_migration_is_idempotent_and_preserves_legacy_rows():
    with tempfile.TemporaryDirectory(prefix="spendguard-fleet-legacy-") as legacy_home_text:
        legacy_home = pathlib.Path(legacy_home_text)
        config.HOME = legacy_home
        with sqlite3.connect(str(legacy_home / "fleet.db")) as connection:
            connection.execute(LEGACY_TABLE_SQL)
            connection.execute(
                "INSERT INTO consumers(pid,served_sha,cmdline,pid_create_time) VALUES(?,?,?,?)",
                (LEGACY_PID, STALE_SHA, FIXTURE_COMMAND, repr(RECORDED_CREATE_TIME)),
            )
        fleet._fleet_db().close()
        fleet._fleet_db().close()
        with sqlite3.connect(str(legacy_home / "fleet.db")) as connection:
            role_columns = [row[1] for row in connection.execute("PRAGMA table_info(consumers)")
                            if row[1] == ROLE_COLUMN_NAME]
            preserved = connection.execute(
                "SELECT COUNT(*) FROM consumers WHERE pid=?", (LEGACY_PID,)
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            migrated = {consumer["pid"]: consumer for consumer in fleet.running_consumers(prune=False)}
        assert role_columns == [ROLE_COLUMN_NAME]
        assert preserved == 1
        assert migrated[LEGACY_PID][ROLE_COLUMN_NAME] == fleet.CONSUMER_ROLE_DAEMON


def test_consumer_registration_installs_one_exit_hook_per_process():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    callbacks = []
    try:
        with patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)):
            fleet.register_consumer()
            fleet._registered_pid = None
            fleet.register_consumer()
        assert len(callbacks) == 1
        assert callbacks[0][0] is fleet._deregister_consumer_at_exit
        assert callbacks[0][1] == os.getpid()
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_unspecified_registration_is_corrected_by_later_explicit_ephemeral_role():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    callbacks = []
    try:
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            fleet.register_consumer()
            with fleet._fleet_db() as connection:
                implicit_role = connection.execute(
                    f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?",
                    (os.getpid(),),
                ).fetchone()[0]
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_EPHEMERAL)
        with fleet._fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?",
                (os.getpid(),),
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            restart_result = fleet.restart_stale()
            warned_pids = {warning["pid"] for warning in restart_result["warn"]}
            transient_pids = {
                consumer["pid"] for consumer in restart_result[fleet.FLEET_RESTART_TRANSIENT_KEY]
            }
        assert implicit_role == fleet.CONSUMER_ROLE_UNSPECIFIED
        assert stored_role == fleet.CONSUMER_ROLE_EPHEMERAL
        assert os.getpid() not in warned_pids
        assert os.getpid() in transient_pids
        assert len(callbacks) == 1
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_concurrent_declared_daemon_is_not_downgraded_by_ephemeral_registration():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    daemon_reached_db_write = threading.Event()
    second_registration_reached_db = threading.Event()
    database_open_count_lock = threading.Lock()
    database_open_count = 0
    insert_statements = []
    callbacks = []
    thread_failures = []
    unpatched_fleet_db = fleet._fleet_db

    def open_interleaved_registration_db():
        nonlocal database_open_count
        with database_open_count_lock:
            database_open_count += 1
            current_open_count = database_open_count
        if current_open_count == 1:
            daemon_reached_db_write.set()
            second_registration_reached_db.wait(CONCURRENT_REGISTRATION_OBSERVATION_TIMEOUT_S)
        elif current_open_count == 2:
            second_registration_reached_db.set()
        return _RegistrationTracingConnection(unpatched_fleet_db(), insert_statements)

    def register_declared_consumer_role(declared_role):
        try:
            fleet.register_consumer(role=declared_role)
        except BaseException as exc:
            thread_failures.append(exc)

    try:
        with patch.object(fleet, "_fleet_db", open_interleaved_registration_db), \
                patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            daemon_thread = threading.Thread(
                target=register_declared_consumer_role,
                args=(fleet.CONSUMER_ROLE_DAEMON,),
            )
            daemon_thread.start()
            assert daemon_reached_db_write.wait(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S), "daemon registration never reached database"
            ephemeral_thread = threading.Thread(
                target=register_declared_consumer_role,
                args=(fleet.CONSUMER_ROLE_EPHEMERAL,),
            )
            ephemeral_thread.start()
            ephemeral_thread.join(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S)
            assert not ephemeral_thread.is_alive(), "ephemeral registration did not finish"
            daemon_thread.join(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S)
            assert not daemon_thread.is_alive(), "daemon registration did not finish"

        assert not thread_failures
        with unpatched_fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?",
                (os.getpid(),),
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            restart_result = fleet.restart_stale()
        assert stored_role == fleet.CONSUMER_ROLE_DAEMON
        assert os.getpid() in {warning["pid"] for warning in restart_result["warn"]}
        assert os.getpid() not in {
            consumer["pid"] for consumer in restart_result[fleet.FLEET_RESTART_TRANSIENT_KEY]
        }
        assert len(insert_statements) == 1
        assert len(callbacks) == 1
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_declared_daemon_is_not_downgraded_by_later_ephemeral_registration():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    try:
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *_args: None), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON)
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_EPHEMERAL)
        with fleet._fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?", (os.getpid(),)
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            restart_result = fleet.restart_stale()
        assert stored_role == fleet.CONSUMER_ROLE_DAEMON
        assert os.getpid() in {warning["pid"] for warning in restart_result["warn"]}
        assert os.getpid() not in {
            consumer["pid"] for consumer in restart_result[fleet.FLEET_RESTART_TRANSIENT_KEY]
        }
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_concurrent_ephemeral_insert_is_upgraded_by_declared_daemon():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    ephemeral_reached_insert = threading.Event()
    allow_ephemeral_insert = threading.Event()
    insert_statements = []
    callbacks = []
    thread_failures = []
    unpatched_fleet_db = fleet._fleet_db
    database_open_count = 0

    def open_ephemeral_first_registration_db():
        nonlocal database_open_count
        database_open_count += 1
        if database_open_count == 1:
            ephemeral_reached_insert.set()
            allow_ephemeral_insert.wait(CONCURRENT_REGISTRATION_OBSERVATION_TIMEOUT_S)
        return _RegistrationTracingConnection(unpatched_fleet_db(), insert_statements)

    def register_concurrent_role_for_ephemeral_first_case(declared_role):
        try:
            fleet.register_consumer(role=declared_role)
        except BaseException as exc:
            thread_failures.append(exc)

    try:
        with patch.object(fleet, "_fleet_db", open_ephemeral_first_registration_db), \
                patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            ephemeral_thread = threading.Thread(
                target=register_concurrent_role_for_ephemeral_first_case,
                args=(fleet.CONSUMER_ROLE_EPHEMERAL,),
            )
            ephemeral_thread.start()
            assert ephemeral_reached_insert.wait(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S), "ephemeral registration never reached database"
            daemon_thread = threading.Thread(
                target=register_concurrent_role_for_ephemeral_first_case,
                args=(fleet.CONSUMER_ROLE_DAEMON,),
            )
            daemon_thread.start()
            allow_ephemeral_insert.set()
            ephemeral_thread.join(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S)
            assert not ephemeral_thread.is_alive(), "ephemeral registration did not finish"
            daemon_thread.join(CONCURRENT_REGISTRATION_COMPLETION_TIMEOUT_S)
            assert not daemon_thread.is_alive(), "daemon registration did not finish"
        assert not thread_failures
        with unpatched_fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?", (os.getpid(),)
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            restart_result = fleet.restart_stale()
        assert stored_role == fleet.CONSUMER_ROLE_DAEMON
        assert os.getpid() in {warning["pid"] for warning in restart_result["warn"]}
        assert os.getpid() not in {
            consumer["pid"] for consumer in restart_result[fleet.FLEET_RESTART_TRANSIENT_KEY]
        }
        assert len(insert_statements) == 1
        assert len(callbacks) == 1
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_unspecified_and_ephemeral_roles_upgrade_to_declared_daemon():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    for initial_role in (None, fleet.CONSUMER_ROLE_EPHEMERAL):
        original_registered_pid = fleet._registered_pid
        original_hook_pid = fleet._consumer_deregister_hook_pid
        fleet._registered_pid = None
        fleet._consumer_deregister_hook_pid = None
        try:
            with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                    patch.object(fleet.atexit, "register", lambda *_args: None):
                fleet.register_consumer(role=initial_role)
                fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON)
            with fleet._fleet_db() as connection:
                stored_role = connection.execute(
                    f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?", (os.getpid(),)
                ).fetchone()[0]
            assert stored_role == fleet.CONSUMER_ROLE_DAEMON
        finally:
            fleet._registered_pid = original_registered_pid
            fleet._consumer_deregister_hook_pid = original_hook_pid


def test_unspecified_only_registration_is_warned_when_stale():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    try:
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *_args: None), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            fleet.register_consumer()
        with fleet._fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?", (os.getpid(),)
            ).fetchone()[0]
        with patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(release, "green_pointer", lambda: {"sha": GREEN_SHA, "short": GREEN_SHA}):
            restart_result = fleet.restart_stale()
        assert stored_role == fleet.CONSUMER_ROLE_UNSPECIFIED
        assert os.getpid() in {warning["pid"] for warning in restart_result["warn"]}
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_later_implicit_registration_preserves_explicit_daemon_role():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    try:
        with patch.object(fleet.atexit, "register", lambda *_args: None):
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON)
            fleet.register_consumer()
        with fleet._fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?",
                (os.getpid(),),
            ).fetchone()[0]
        assert stored_role == fleet.CONSUMER_ROLE_DAEMON
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_transient_consumer_insert_failure_remains_eligible_for_retry():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    unpatched_fleet_db = fleet._fleet_db
    database_open_count = 0
    insert_statements = []
    callbacks = []

    def open_fail_first_registration_db():
        nonlocal database_open_count
        database_open_count += 1
        connection = unpatched_fleet_db()
        if database_open_count == 1:
            return _FailingConsumerInsertConnection(connection)
        return _RegistrationTracingConnection(connection, insert_statements)

    try:
        with patch.object(fleet, "_fleet_db", open_fail_first_registration_db), \
                patch.object(fleet.os, "getpid", lambda: TRANSIENT_RETRY_PID), \
                patch.object(fleet.psutil, "Process", lambda _pid: _RoleFixtureProcess()), \
                patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_EPHEMERAL)
            assert fleet._registered_pid is None
            with unpatched_fleet_db() as connection:
                assert connection.execute(
                    "SELECT COUNT(*) FROM consumers WHERE pid=?", (TRANSIENT_RETRY_PID,)
                ).fetchone()[0] == 0
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_EPHEMERAL)
            fleet.register_consumer()

        with unpatched_fleet_db() as connection:
            stored_role = connection.execute(
                f"SELECT {fleet.CONSUMER_ROLE_COLUMN} FROM consumers WHERE pid=?", (TRANSIENT_RETRY_PID,)
            ).fetchone()[0]
        assert fleet._registered_pid == TRANSIENT_RETRY_PID
        assert stored_role == fleet.CONSUMER_ROLE_EPHEMERAL
        assert len(insert_statements) == 1
        assert len(callbacks) == 1
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


def test_forked_child_registers_own_identity_and_exit_hook():
    registry_home = pathlib.Path(os.environ["SPENDGUARD_HOME"])
    config.HOME = registry_home
    original_registered_pid = fleet._registered_pid
    original_hook_pid = fleet._consumer_deregister_hook_pid
    fleet._registered_pid = None
    fleet._consumer_deregister_hook_pid = None
    simulated_pid = FORK_PARENT_PID
    callbacks = []

    class _ForkIdentityProcess:
        def __init__(self, pid):
            self._pid = pid

        def create_time(self):
            if self._pid == FORK_PARENT_PID:
                return RECORDED_CREATE_TIME
            return FORK_CHILD_CREATE_TIME

    try:
        with patch.object(fleet.os, "getpid", lambda: simulated_pid), \
                patch.object(fleet.psutil, "Process", _ForkIdentityProcess), \
                patch.object(fleet.atexit, "register", lambda *args: callbacks.append(args)), \
                patch.object(release, "served_sha", lambda: {"sha": STALE_SHA, "short": STALE_SHA}):
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON)
            simulated_pid = FORK_CHILD_PID
            fleet.register_consumer(role=fleet.CONSUMER_ROLE_EPHEMERAL)

        with fleet._fleet_db() as connection:
            stored_rows = connection.execute(
                f"SELECT pid,pid_create_time,{fleet.CONSUMER_ROLE_COLUMN} FROM consumers "
                "WHERE pid IN (?,?) ORDER BY pid",
                (FORK_PARENT_PID, FORK_CHILD_PID),
            ).fetchall()
        assert stored_rows == [
            (FORK_PARENT_PID, repr(RECORDED_CREATE_TIME), fleet.CONSUMER_ROLE_DAEMON),
            (FORK_CHILD_PID, repr(FORK_CHILD_CREATE_TIME), fleet.CONSUMER_ROLE_EPHEMERAL),
        ]
        assert [callback[1] for callback in callbacks] == [FORK_PARENT_PID, FORK_CHILD_PID]
        assert fleet._registered_pid == FORK_CHILD_PID
        assert fleet._consumer_deregister_hook_pid == FORK_CHILD_PID
    finally:
        fleet._registered_pid = original_registered_pid
        fleet._consumer_deregister_hook_pid = original_hook_pid


if __name__ == "__main__":
    cases = [case for name, case in sorted(globals().items()) if name.startswith("test_") and callable(case)]
    for case in cases:
        case()
        print(f"  [OK] {case.__name__}")
    print(f"\nOK test_fleet_consumer_role_lifecycle: {len(cases)} test(s) passed")
