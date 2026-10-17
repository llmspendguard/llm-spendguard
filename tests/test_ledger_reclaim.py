"""Offline data-preservation guards for ledger copy/verify/swap reclamation."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch


RECLAIM_TEST_ROWS = 256
RECLAIM_TEST_RETAINED = 16
RECLAIM_TEST_PAYLOAD_BYTES = 4096
RECLAIM_TEST_WRITE_PERMISSION_BITS = 0o222
RECLAIM_TEST_AUTO_VACUUM_MODES = {"none": 0, "full": 1, "incremental": 2}
RECLAIM_TEST_LOCK_PROBE = """import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0)
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    print(error)
    sys.exit(0 if error.sqlite_errorcode == sqlite3.SQLITE_BUSY else 2)
else:
    connection.rollback()
    sys.exit(1)
finally:
    connection.close()
"""
RECLAIM_TEST_ROOT = Path(__file__).resolve().parents[1]
RECLAIM_TEST_SCRIPT = RECLAIM_TEST_ROOT / "scripts" / "ops" / "ledger_reclaim.py"

reclaim_test_spec = importlib.util.spec_from_file_location("ledger_reclaim_under_test", RECLAIM_TEST_SCRIPT)
reclaim_test_module = importlib.util.module_from_spec(reclaim_test_spec)
sys.modules[reclaim_test_spec.name] = reclaim_test_module
reclaim_test_spec.loader.exec_module(reclaim_test_module)
reclaim_test_unwrapped = reclaim_test_module.reclaim_ledger


def ledger_reclaim_test_quiet_call(reports, *args, **kwargs):
    transcript = io.StringIO()
    try:
        with contextlib.redirect_stdout(transcript):
            return reclaim_test_unwrapped(*args, **kwargs)
    finally:
        reports.append(transcript.getvalue())




def ledger_reclaim_test_check(label, condition, failures):
    print(f"[{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def ledger_reclaim_test_seed(directory, wal=False):
    database = directory / "fixture.sqlite"
    with sqlite3.connect(database) as connection:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute('CREATE TABLE "ledger records" (id INTEGER PRIMARY KEY, payload BLOB)')
        connection.executemany('INSERT INTO "ledger records" (payload) VALUES (?)',
                               [(bytes(RECLAIM_TEST_PAYLOAD_BYTES),)] * RECLAIM_TEST_ROWS)
        connection.execute('CREATE TABLE metadata (value TEXT)')
        connection.execute("INSERT INTO metadata VALUES ('retained')")
        connection.commit()
        connection.execute('DELETE FROM "ledger records" WHERE id > ?', (RECLAIM_TEST_RETAINED,))
    connection.close()
    return database


def ledger_reclaim_test_state(database):
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
    connection.close()
    return database.read_bytes(), database.stat().st_mtime_ns, integrity


def ledger_reclaim_test_refusal(label, options, failures, reports):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = ledger_reclaim_test_seed(root)
        backup = root / "backups"
        backup.mkdir()
        before = ledger_reclaim_test_state(database)
        kwargs = dict(backup_dir=backup, execute=True, client_probe=lambda path: False)
        options(root, database, kwargs)
        refused = False
        try:
            ledger_reclaim_test_quiet_call(reports, database, **kwargs)
        except reclaim_test_module.LedgerReclaimRefusal:
            refused = True
        ledger_reclaim_test_check(label, refused and ledger_reclaim_test_state(database) == before, failures)


def run_ledger_reclaim_guards(failures):
    reports = []
    ledger_reclaim_test_refusal("missing backup path refuses without changing source",
                               lambda root, database, kwargs: kwargs.update(backup_dir=None), failures, reports)
    ledger_reclaim_test_refusal("non-directory backup refuses without changing source",
                               lambda root, database, kwargs: kwargs.update(backup_dir=database), failures, reports)
    ledger_reclaim_test_refusal("bad source integrity refuses without changing source",
                               lambda root, database, kwargs: kwargs.update(
                                   integrity_reader=lambda connection: [("simulated corruption",)]), failures, reports)
    ledger_reclaim_test_refusal("insufficient free disk refuses without changing source",
                               lambda root, database, kwargs: kwargs.update(free_bytes=lambda path: 0), failures, reports)
    ledger_reclaim_test_refusal("attached idle client refuses without changing source",
                               lambda root, database, kwargs: kwargs.update(client_probe=lambda path: True), failures, reports)

    with tempfile.TemporaryDirectory() as reclaim_test_directory:
        reclaim_test_root = Path(reclaim_test_directory)
        reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root)
        reclaim_test_backup = reclaim_test_root / "backups"
        reclaim_test_backup.mkdir()
        reclaim_test_before = ledger_reclaim_test_state(reclaim_test_database)
        reclaim_test_original_mode = reclaim_test_backup.stat().st_mode
        reclaim_test_backup.chmod(reclaim_test_original_mode & ~RECLAIM_TEST_WRITE_PERMISSION_BITS)
        reclaim_test_refused = False
        try:
            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup,
                                              execute=True, client_probe=lambda path: False)
        except reclaim_test_module.LedgerReclaimRefusal:
            reclaim_test_refused = True
        finally:
            reclaim_test_backup.chmod(reclaim_test_original_mode)
        ledger_reclaim_test_check("unwritable backup directory refuses and source survives",
                                  reclaim_test_refused and ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before, failures)


    def ledger_reclaim_test_mismatched_copy(connection, destination):
        connection.execute("VACUUM INTO ?", (str(destination),))
        with sqlite3.connect(destination) as copy_connection:
            copy_connection.execute('DELETE FROM "ledger records" WHERE id = (SELECT MIN(id) FROM "ledger records")')
        copy_connection.close()


    ledger_reclaim_test_refusal("row-count mismatch blocks swap and original survives",
                               lambda root, database, kwargs: kwargs.update(compact_copy=ledger_reclaim_test_mismatched_copy), failures, reports)

    with tempfile.TemporaryDirectory() as reclaim_test_directory:
        reclaim_test_root = Path(reclaim_test_directory)
        reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root)
        reclaim_test_backup = reclaim_test_root / "backups"
        reclaim_test_backup.mkdir()
        reclaim_test_before = ledger_reclaim_test_state(reclaim_test_database)
        reclaim_test_writer = sqlite3.connect(reclaim_test_database)
        reclaim_test_writer.execute("BEGIN IMMEDIATE")
        reclaim_test_refused = False
        try:
            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup,
                                              execute=True, client_probe=lambda path: False)
        except reclaim_test_module.LedgerReclaimRefusal:
            reclaim_test_refused = True
        finally:
            reclaim_test_writer.rollback()
            reclaim_test_writer.close()
        ledger_reclaim_test_check("held SQLite writer lock refuses and source survives",
                                  reclaim_test_refused and ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before, failures)

    for reclaim_test_wal in (False, True):
        with tempfile.TemporaryDirectory() as reclaim_test_directory:
            reclaim_test_root = Path(reclaim_test_directory)
            reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root, wal=reclaim_test_wal)
            reclaim_test_backup = reclaim_test_root / "backups"
            reclaim_test_backup.mkdir()
            reclaim_test_before = ledger_reclaim_test_state(reclaim_test_database)
            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup,
                                              client_probe=lambda path: False)
            ledger_reclaim_test_check(f"dry-run is byte/mtime/integrity identical (WAL={reclaim_test_wal})",
                                      ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before, failures)
            reclaim_test_statements = []
            reclaim_test_original_opener = reclaim_test_module.config.open_ledger_maintenance_connection

            def ledger_reclaim_test_traced_opener(*args, _opener=reclaim_test_original_opener,
                                                 _statements=reclaim_test_statements, **kwargs):
                traced_connection = _opener(*args, **kwargs)
                traced_connection.set_trace_callback(_statements.append)
                return traced_connection

            with patch.object(reclaim_test_module.config, "open_ledger_maintenance_connection",
                              side_effect=ledger_reclaim_test_traced_opener):
                reclaim_test_report = ledger_reclaim_test_quiet_call(reports, 
                    reclaim_test_database, backup_dir=reclaim_test_backup, execute=True, client_probe=lambda path: False)
            ledger_reclaim_test_check(f"post-swap PRAGMA optimize runs (WAL={reclaim_test_wal})",
                                      "PRAGMA optimize" in reclaim_test_statements, failures)
            with sqlite3.connect(reclaim_test_database) as reclaim_test_connection:
                reclaim_test_count = reclaim_test_connection.execute('SELECT COUNT(*) FROM "ledger records"').fetchone()[0]
                reclaim_test_metadata = reclaim_test_connection.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
                reclaim_test_integrity = reclaim_test_connection.execute("PRAGMA integrity_check").fetchall()
            reclaim_test_connection.close()
            ledger_reclaim_test_check(f"happy path reclaims and preserves all table counts (WAL={reclaim_test_wal})",
                                      reclaim_test_database.stat().st_size < len(reclaim_test_before[0])
                                      and reclaim_test_count == RECLAIM_TEST_RETAINED and reclaim_test_metadata == 1
                                      and reclaim_test_integrity == [("ok",)], failures)
            reclaim_test_kept = Path(reclaim_test_report["backup"])
            with sqlite3.connect(f"{reclaim_test_kept.as_uri()}?mode=ro", uri=True) as reclaim_test_connection:
                reclaim_test_kept_counts = reclaim_test_module.ledger_user_counts(reclaim_test_connection)
                reclaim_test_kept_integrity = reclaim_test_connection.execute("PRAGMA integrity_check").fetchall()
            reclaim_test_connection.close()
            ledger_reclaim_test_check(f"original backup retains all data (WAL={reclaim_test_wal})",
                                      reclaim_test_kept_counts == {"ledger records": RECLAIM_TEST_RETAINED, "metadata": 1}
                                      and reclaim_test_kept_integrity == [("ok",)]
                                      and (reclaim_test_wal or reclaim_test_kept.read_bytes() == reclaim_test_before[0]), failures)

    for reclaim_test_mode, reclaim_test_mode_value in RECLAIM_TEST_AUTO_VACUUM_MODES.items():
        with tempfile.TemporaryDirectory() as reclaim_test_directory:
            reclaim_test_root = Path(reclaim_test_directory)
            reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root)
            reclaim_test_backup = reclaim_test_root / "backups"
            reclaim_test_backup.mkdir()
            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup, execute=True,
                                              client_probe=lambda path: False, auto_vacuum=reclaim_test_mode)
            with sqlite3.connect(reclaim_test_database) as reclaim_test_connection:
                reclaim_test_actual_mode = reclaim_test_connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            reclaim_test_connection.close()
            ledger_reclaim_test_check(f"explicit auto_vacuum={reclaim_test_mode} takes effect",
                                      reclaim_test_actual_mode == reclaim_test_mode_value, failures)

    for reclaim_test_wal in (False, True):
        with tempfile.TemporaryDirectory() as reclaim_test_directory:
            reclaim_test_root = Path(reclaim_test_directory)
            reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root, wal=reclaim_test_wal)
            reclaim_test_backup = reclaim_test_root / "backups"
            reclaim_test_backup.mkdir()
            reclaim_test_lock_results = []

            def ledger_reclaim_test_locked_copy(connection, destination, _database=reclaim_test_database,
                                                _results=reclaim_test_lock_results):
                probe = subprocess.run([sys.executable, "-c", RECLAIM_TEST_LOCK_PROBE, str(_database)],
                                       capture_output=True, text=True, env=os.environ.copy())
                _results.append(probe.returncode)
                connection.execute("VACUUM INTO ?", (str(destination),))

            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup, execute=True,
                                              client_probe=lambda path: False, compact_copy=ledger_reclaim_test_locked_copy)
            ledger_reclaim_test_check(f"exclusive writer exclusion survives WAL detach (WAL={reclaim_test_wal})",
                                      reclaim_test_lock_results == [0], failures)

    with tempfile.TemporaryDirectory() as reclaim_test_directory:
        reclaim_test_root = Path(reclaim_test_directory)
        reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root)
        reclaim_test_backup = reclaim_test_root / "backups"
        reclaim_test_backup.mkdir()
        reclaim_test_before = ledger_reclaim_test_state(reclaim_test_database)
        reclaim_test_probe_calls = []

        def ledger_reclaim_test_late_client(path):
            reclaim_test_probe_calls.append(path)
            return len(reclaim_test_probe_calls) > 1

        reclaim_test_refused = False
        try:
            ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup,
                                              execute=True, client_probe=ledger_reclaim_test_late_client)
        except reclaim_test_module.LedgerReclaimRefusal:
            reclaim_test_refused = True
        ledger_reclaim_test_check("client appearing at final probe prevents swap",
                                  reclaim_test_refused and ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before, failures)
        reclaim_test_other = reclaim_test_root / "other.sqlite"
        reclaim_test_other.write_bytes(reclaim_test_before[0])
        reclaim_test_refused = False
        try:
            reclaim_test_module.ledger_no_replace_renamer()(reclaim_test_other, reclaim_test_database)
        except OSError:
            reclaim_test_refused = True
        ledger_reclaim_test_check("atomic no-replace rename preserves existing target and source",
                                  reclaim_test_refused and reclaim_test_other.read_bytes() == reclaim_test_before[0]
                                  and ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before, failures)

    reclaim_test_help = subprocess.run([sys.executable, str(RECLAIM_TEST_SCRIPT), "--help"],
                                      capture_output=True, text=True, env=os.environ.copy())
    ledger_reclaim_test_check("CLI help parses", reclaim_test_help.returncode == 0, failures)
    with tempfile.TemporaryDirectory() as reclaim_test_directory:
        reclaim_test_root = Path(reclaim_test_directory)
        reclaim_test_database = ledger_reclaim_test_seed(reclaim_test_root)
        reclaim_test_backup = reclaim_test_root / "backups"
        reclaim_test_backup.mkdir()
        reclaim_test_before = ledger_reclaim_test_state(reclaim_test_database)
        reclaim_test_demo = subprocess.run(
            [sys.executable, str(RECLAIM_TEST_SCRIPT), "--ledger", str(reclaim_test_database),
             "--backup-dir", str(reclaim_test_backup)], capture_output=True, text=True, env=os.environ.copy())
        print(reclaim_test_demo.stdout, end="")
        print(reclaim_test_demo.stderr, end="")
        ledger_reclaim_test_check("CLI temporary dry-run is byte/mtime identical and creates no backups",
                                  reclaim_test_demo.returncode == 0
                                  and ledger_reclaim_test_state(reclaim_test_database) == reclaim_test_before
                                  and not list(reclaim_test_backup.iterdir()), failures)
        ledger_reclaim_test_check("Spotlight exclusion defaults off",
                                  not (reclaim_test_root / reclaim_test_module.SPOTLIGHT_MARKER).exists(), failures)
        ledger_reclaim_test_quiet_call(reports, reclaim_test_database, backup_dir=reclaim_test_backup, execute=True,
                                          client_probe=lambda path: False, exclude_spotlight=True)
        ledger_reclaim_test_check("explicit Spotlight exclusion creates derived-directory marker",
                                  (reclaim_test_root / reclaim_test_module.SPOTLIGHT_MARKER).is_file(), failures)


if __name__ == "__main__":
    failures = []
    run_ledger_reclaim_guards(failures)
    sys.exit(bool(failures))
