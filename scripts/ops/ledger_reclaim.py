"""Offline, fail-closed ledger reclaim. Dry-run unless --execute is supplied.

The retained original is never deleted, including on installation failure.
POSIX and lsof are required: an idle WAL client is unsafe even without a txn.
"""
import argparse
import contextlib
import ctypes
import ctypes.util
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid

from spendguard import config

# One GiB reserve, in addition to every live page: 1024 ** 3 bytes. Covers
# filesystem/journal overhead; CLI may increase it for a constrained filesystem.
DEFAULT_DISK_MARGIN_BYTES = 1024 ** 3
RECLAIM_BUSY_TIMEOUT_SECONDS = 0  # Offline means never wait/fight for a writer.
SPOTLIGHT_MARKER = ".metadata_never_index"
AUTO_VACUUM_MODES = {"none": 0, "full": 1, "incremental": 2}
LSOF_COMMAND = "lsof"
DARWIN_RENAME_EXCL = 0x00000004
LINUX_RENAME_NOREPLACE = 1
LINUX_AT_FDCWD = -100


class LedgerReclaimRefusal(RuntimeError):
    """A safety condition was absent; no unverified copy may be installed."""


def ledger_integrity_rows(connection):
    return connection.execute("PRAGMA integrity_check").fetchall()


def ledger_require_integrity(connection, reader):
    rows = reader(connection)
    if rows != [("ok",)]:
        raise LedgerReclaimRefusal(f"integrity_check failed: {rows!r}")


def ledger_attached_clients(path, *, allow_own_handle=False):
    """Conservative: ANY open client (including idle writers) refuses reclaim.

    lsof must be installed and must complete normally; an inspection error is
    not permission. SQLite's retained exclusive lock closes the subsequent race.
    """
    executable = shutil.which(LSOF_COMMAND)
    if executable is None or os.name != "posix":
        raise LedgerReclaimRefusal("cannot prove offline: POSIX lsof required")
    paths = [path] + [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
    result = subprocess.run([executable, "-t", "--", *map(str, filter(Path.exists, paths))],
                            capture_output=True, text=True, check=False)
    if result.stderr or result.returncode not in (0, 1):
        raise LedgerReclaimRefusal(f"cannot inspect attached writers: {result.stderr}")
    return any(not allow_own_handle or int(pid) != os.getpid() for pid in result.stdout.split())


def ledger_no_replace_renamer():
    """Require the OS's atomic *non-overwriting* rename, never os.rename.

    During the two-rename gap a newly started client could create the source
    pathname. Ordinary POSIX rename would silently destroy that client's data.
    """
    library = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        function = library.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = lambda old, new: (os.fsencode(old), os.fsencode(new), DARWIN_RENAME_EXCL)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        arguments = lambda old, new: (LINUX_AT_FDCWD, os.fsencode(old), LINUX_AT_FDCWD,
                                      os.fsencode(new), LINUX_RENAME_NOREPLACE)
    else:
        raise LedgerReclaimRefusal("OS has no supported atomic no-replace rename")
    function.restype = ctypes.c_int

    def ledger_rename_without_overwrite(old, new):
        if function(*arguments(old, new)):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(new))
    return ledger_rename_without_overwrite


def ledger_user_counts(connection):
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()
    return {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"')
            .fetchone()[0] for (name,) in tables}


def ledger_reclaim_fsync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reclaim_ledger(path, backup_dir=None, *, execute=False,
                   margin_bytes=DEFAULT_DISK_MARGIN_BYTES, free_bytes=None,
                   integrity_reader=ledger_integrity_rows, compact_copy=None,
                   client_probe=None, auto_vacuum=None, exclude_spotlight=False):
    """Return full report or raise; seams support offline fault-injection tests.

    An exclusive SQLite handle remains open through install. WAL is checkpointed
    then switched to DELETE under that lock: otherwise closing an old WAL handle
    after replacement could remove the replacement's WAL sidecars. No in-place
    VACUUM is ever issued. Original data remains recoverable at every boundary.
    """
    source = Path(path).absolute()
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise LedgerReclaimRefusal("source must be an existing, unaliased regular ledger")
    source_start = source.stat()
    if margin_bytes < 0:
        raise LedgerReclaimRefusal("disk margin must be nonnegative")
    if auto_vacuum is not None and auto_vacuum not in AUTO_VACUUM_MODES:
        raise LedgerReclaimRefusal("invalid auto_vacuum mode")
    if backup_dir is None:
        raise LedgerReclaimRefusal("an explicit verified --backup-dir is required")
    destination = Path(backup_dir).resolve(strict=True)
    if (not destination.is_dir() or not destination.stat().st_mode &
            (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) or
            not os.access(destination, os.W_OK | os.X_OK)):
        raise LedgerReclaimRefusal("backup directory is not writable/searchable")
    if source.stat().st_dev != destination.stat().st_dev:
        raise LedgerReclaimRefusal("backup and ledger must share a filesystem for atomic rename")
    attached = (client_probe or ledger_attached_clients)(source)
    if attached:
        print(json.dumps({"source": str(source), "writer_attached": True, "execute": execute}))
        raise LedgerReclaimRefusal("writer/client attached; stop the warm daemon and all ledger clients")
    # Cold WAL reads can create -shm even in mode=ro. Refuse rather than let a
    # dry-run create a sidecar; a human must bring the ledger fully offline.
    wal = Path(str(source) + "-wal")
    shm = Path(str(source) + "-shm")
    if wal.exists() and wal.stat().st_size and not shm.exists():
        raise LedgerReclaimRefusal("cold WAL without shared memory: cannot inspect without mutation")
    with contextlib.closing(config.open_ledger_maintenance_connection(source)) as inspection:
        ledger_require_integrity(inspection, integrity_reader)
        page_size = inspection.execute("PRAGMA page_size").fetchone()[0]
        page_count = inspection.execute("PRAGMA page_count").fetchone()[0]
        freelist = inspection.execute("PRAGMA freelist_count").fetchone()[0]
        current_auto = inspection.execute("PRAGMA auto_vacuum").fetchone()[0]
    live_bytes = (page_count - freelist) * page_size
    available = (free_bytes or (lambda directory: shutil.disk_usage(directory).free))(destination)
    required = live_bytes + margin_bytes
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    token = f"{stamp}-{uuid.uuid4().hex}"
    operation = destination / f"{source.name}.reclaim-{token}"
    compacted = operation / f"{source.name}.compacted-{token}"
    original = operation / f"{source.name}.pre-reclaim-{token}"
    report = dict(source=str(source), file_bytes=source.stat().st_size,
                  page_count=page_count, freelist_count=freelist, page_size=page_size,
                  reclaimable_bytes=freelist * page_size, projected_bytes=live_bytes,
                  free_bytes=available, required_bytes=required, margin_bytes=margin_bytes,
                  writer_attached=attached, auto_vacuum=current_auto,
                  requested_auto_vacuum=auto_vacuum,
                  requested_spotlight_exclusion=exclude_spotlight,
                  spotlight_excluded=(source.parent / SPOTLIGHT_MARKER).exists(),
                  execute=execute, backup=str(original), compacted=str(compacted),
                  actions=["refuse attached clients; acquire retained exclusive SQLite lock",
                           "checkpoint TRUNCATE; require complete; detach WAL safely",
                           f"VACUUM INTO {compacted}",
                           "verify integrity and enumerated per-table row-count parity",
                           f"rename original to {original}; atomically install verified copy",
                           "PRAGMA optimize; retain original forever"])
    print(json.dumps(report, indent=2))
    if available < required:
        raise LedgerReclaimRefusal(f"insufficient free disk: {available} < {required}")
    if not execute:
        return report
    if os.geteuid() == 0:
        raise LedgerReclaimRefusal("execute as an unprivileged ledger owner; root bypasses write exclusion")
    if source.stat().st_uid != os.geteuid():
        raise LedgerReclaimRefusal("execute requires the ledger owner")
    rename_without_overwrite = ledger_no_replace_renamer()
    # A unique private directory prevents accidental overwrite of any prior
    # backup. Both rename destinations are on the checked filesystem.
    installed = False
    with contextlib.closing(config.open_ledger_maintenance_connection(
            source, read_only=False, timeout=RECLAIM_BUSY_TIMEOUT_SECONDS)) as connection:
        try:
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")  # exclusive mode retains the lock outside transactions
        except sqlite3.Error as exc:
            raise LedgerReclaimRefusal(f"writer detected: cannot acquire exclusive lock: {exc}") from exc
        ledger_require_integrity(connection, integrity_reader)
        if (source.stat().st_dev, source.stat().st_ino) != (source_start.st_dev, source_start.st_ino):
            raise LedgerReclaimRefusal("source pathname changed during inspection")
        # Recheck under lock: a transaction could commit after initial inspection.
        locked_live = (connection.execute("PRAGMA page_count").fetchone()[0] -
                       connection.execute("PRAGMA freelist_count").fetchone()[0]) * page_size
        locked_free = (free_bytes or (lambda directory: shutil.disk_usage(directory).free))(destination)
        if locked_free < locked_live + margin_bytes:
            raise LedgerReclaimRefusal("insufficient free disk after exclusive acquisition")
        operation.mkdir(mode=stat.S_IRWXU)
        # Persist the recovery directory's name before the only original is
        # moved into it; syncing the child alone does not persist its parent.
        ledger_reclaim_fsync(destination)
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
            raise LedgerReclaimRefusal(f"writer detected: incomplete checkpoint {checkpoint}")
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise LedgerReclaimRefusal("cannot detach WAL under exclusive lock")
        # INCREMENTAL permits future incremental_vacuum without rebuilding,
        # at the cost of pointer-map storage and small additional write work.
        if auto_vacuum is not None:
            connection.execute(f"PRAGMA auto_vacuum={AUTO_VACUUM_MODES[auto_vacuum]}")
        expected = ledger_user_counts(connection)
        if compact_copy is None:
            from spendguard.budget import snapshot
            snapshot(compact_source=connection, compact_target=compacted)
        else:
            compact_copy(connection, compacted)
        with contextlib.closing(config.open_ledger_maintenance_connection(compacted)) as candidate:
            ledger_require_integrity(candidate, integrity_reader)
            observed = ledger_user_counts(candidate)
            print(json.dumps({"source_counts": expected, "copy_counts": observed}, indent=2))
            if expected != observed:
                raise LedgerReclaimRefusal("VERIFY FAILED: row-count parity; original survives, no swap")
            if auto_vacuum is not None and candidate.execute(
                    "PRAGMA auto_vacuum").fetchone()[0] != AUTO_VACUUM_MODES[auto_vacuum]:
                raise LedgerReclaimRefusal("VERIFY FAILED: auto_vacuum did not take effect")
        os.chmod(compacted, stat.S_IMODE(source.stat().st_mode))
        ledger_reclaim_fsync(compacted)
        ledger_reclaim_fsync(operation)
        # Preserve ownership rather than silently broaden ledger access.
        if (compacted.stat().st_uid, compacted.stat().st_gid) != (
                source.stat().st_uid, source.stat().st_gid):
            raise LedgerReclaimRefusal("copy ownership differs from original")
        # Freeze new writable opens before the final client scan. Clients that
        # opened between the initial scan and chmod are visible to lsof; retained
        # SQLite locks prevent their writes in the meantime. Existing handles
        # cannot be made read-only by chmod, hence this SECOND scan is mandatory.
        source_mode = stat.S_IMODE(source.stat().st_mode)
        source_identity = source.stat().st_ino
        os.chmod(source, source_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        try:
            ledger_reclaim_fsync(source)
            final_attached = (client_probe(source) if client_probe else
                              ledger_attached_clients(source, allow_own_handle=True))
            if final_attached:
                raise LedgerReclaimRefusal("writer/client appeared during reclaim; refusing swap")
            rename_without_overwrite(source, original)
            ledger_reclaim_fsync(original)
            ledger_reclaim_fsync(operation)
            ledger_reclaim_fsync(source.parent)
            rename_without_overwrite(compacted, source)
            installed = True
            ledger_reclaim_fsync(source.parent)
            ledger_reclaim_fsync(operation)
        except OSError as exc:
            # Never remove/overwrite the kept original during recovery. The
            # error explicitly names the recovery path, including after crash.
            raise LedgerReclaimRefusal(f"install failed; retained original: {original}: {exc}") from exc
        finally:
            if source.exists() and source.stat().st_ino == source_identity:
                os.chmod(source, source_mode)
    if installed:
        with contextlib.closing(config.open_ledger_maintenance_connection(source, read_only=False)) as fresh:
            config.tune_ledger_connection(fresh)
            fresh.execute("PRAGMA optimize")
        if exclude_spotlight:
            (source.parent / SPOTLIGHT_MARKER).touch(exist_ok=True)
        report.update(post_bytes=source.stat().st_size, counts=expected)
        print(json.dumps(report, indent=2))
    return report


def ledger_reclaim_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, help="override config.db_path (e.g. a temporary test ledger)")
    parser.add_argument("--backup-dir", type=Path, help="existing writable directory on the ledger filesystem")
    parser.add_argument("--margin-bytes", type=int, default=DEFAULT_DISK_MARGIN_BYTES)
    parser.add_argument("--execute", action="store_true", help="perform verified reclaim; default is read-only")
    parser.add_argument("--set-auto-vacuum", choices=AUTO_VACUUM_MODES)
    parser.add_argument("--exclude-spotlight", action="store_true")
    options = parser.parse_args(argv)
    try:
        reclaim_ledger(options.ledger or config.db_path(), options.backup_dir,
                       execute=options.execute, margin_bytes=options.margin_bytes,
                       auto_vacuum=options.set_auto_vacuum,
                       exclude_spotlight=options.exclude_spotlight)
    except (LedgerReclaimRefusal, OSError, sqlite3.Error) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(ledger_reclaim_cli())
