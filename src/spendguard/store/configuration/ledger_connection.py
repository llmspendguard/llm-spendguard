"""Ledger connection ownership, isolation, pooling, and lifecycle controls."""

import contextlib
import gc
import math
import os
import sqlite3
import sys
import threading
import time
import weakref
from functools import lru_cache, partialmethod
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from spendguard.store.configuration.config_store import (
    DEFAULT_SPENDGUARD_HOME,
    HOME,
    LEDGER_HOME_ENVIRONMENT_KEY,
    LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY,
    _cfg_get,
)
from spendguard.store.spend_search.ledger_read_accessors import resolve_ledger_admission_deadline
from spendguard.store.spend_search.ledger_request_cursor_accessors import make_managed_request_cursor_state
from spendguard.store.money_ledger.ledger_writer_accessors import (
    resolve_active_ledger_writer_connection,
    run_wired_ledger_write,
    acquire_ledger_writer_deadline_lock,
    abort_active_ledger_writer_transaction,
    probe_ledger_connection_commit_deferred,
    require_live_ledger_writer_request,
    enter_isolated_nested_ledger_write,
)


ISOLATED_TEST_ENVIRONMENT_KEY = "SPENDGUARD_TEST_ISOLATED"
ISOLATED_TEST_ENABLED_VALUE = "1"
SQLITE_CONNECT_AUDIT_EVENT = "sqlite3.connect"
SQLITE_MEMORY_DATABASE_NAME = ":memory:"
SQLITE_FILE_URI_SCHEME = "file"
SQLITE_MEMORY_MODE_QUERY_KEY = "mode"
SQLITE_MEMORY_MODE_QUERY_VALUE = "memory"
ISOLATED_SQLITE_ROOT_ENVIRONMENT_KEYS = ("HOME", "SPENDGUARD_HOME", "TMPDIR")
COVERAGE_DATA_FILE_ENVIRONMENT_KEY = "COVERAGE_FILE"
COVERAGE_DEFAULT_DATA_FILE_NAME = ".coverage"
ISOLATED_SQLITE_IMPORT_HOME_ROOT = (
    HOME.expanduser().resolve()
    if os.environ.get(ISOLATED_TEST_ENVIRONMENT_KEY) == ISOLATED_TEST_ENABLED_VALUE
    else None
)


class IsolatedSQLitePathError(RuntimeError):
    """An isolated test tried to open SQLite state outside its scratch roots."""




def isolated_sqlite_target_path(database):
    """Return the resolved file target for a SQLite connect argument, or None for memory databases."""
    try:
        database_text = os.fsdecode(os.fspath(database))
    except TypeError:
        return None
    if database_text == SQLITE_MEMORY_DATABASE_NAME:
        return None
    parsed_database_uri = urlsplit(database_text)
    if parsed_database_uri.scheme == SQLITE_FILE_URI_SCHEME:
        database_query = parse_qs(parsed_database_uri.query)
        if database_query.get(SQLITE_MEMORY_MODE_QUERY_KEY) == [SQLITE_MEMORY_MODE_QUERY_VALUE]:
            return None
        database_text = unquote(parsed_database_uri.path)
        if database_text == SQLITE_MEMORY_DATABASE_NAME:
            return None
    return Path(database_text).expanduser().resolve()


def is_coverage_data_file_path(database_path):
    """Return whether a path follows coverage.py's configured or default data-file convention."""
    configured_data_file = os.environ.get(COVERAGE_DATA_FILE_ENVIRONMENT_KEY)
    if configured_data_file:
        configured_path = Path(configured_data_file).expanduser().resolve()
        return database_path == configured_path or (
            database_path.parent == configured_path.parent
            and database_path.name.startswith(f"{configured_path.name}.")
        )
    return database_path.name.startswith(COVERAGE_DEFAULT_DATA_FILE_NAME)


def validate_isolated_sqlite_open_path(database):
    """Fail closed when an isolated test's SQLite file escapes every declared scratch root."""
    database_path = isolated_sqlite_target_path(database)
    if database_path is None or is_coverage_data_file_path(database_path):
        return
    live_allowed_roots = tuple(
        Path(root_value).expanduser().resolve()
        for environment_key in ISOLATED_SQLITE_ROOT_ENVIRONMENT_KEYS
        if (root_value := os.environ.get(environment_key))
    )
    allowed_roots = live_allowed_roots + (
        (ISOLATED_SQLITE_IMPORT_HOME_ROOT,) if ISOLATED_SQLITE_IMPORT_HOME_ROOT is not None else ()
    )
    if any(database_path == allowed_root or allowed_root in database_path.parents for allowed_root in allowed_roots):
        return
    allowed_root_list = ", ".join(os.fspath(allowed_root) for allowed_root in allowed_roots) or "<none>"
    raise IsolatedSQLitePathError(
        f"isolated SQLite path escaped scratch roots: {database_path}; allowed roots: {allowed_root_list}"
    )


def isolated_sqlite_connect_audit_hook(event, arguments):
    """Audit every Python SQLite open made after spendguard config is imported."""
    if event != SQLITE_CONNECT_AUDIT_EVENT or not arguments:
        return
    if os.environ.get(ISOLATED_TEST_ENVIRONMENT_KEY) != ISOLATED_TEST_ENABLED_VALUE:
        return
    validate_isolated_sqlite_open_path(arguments[0])


# Register the isolated-SQLite confinement hook at IMPORT, at module scope — this is a side-effecting
# statement, not a definition, so a VERBATIM cluster carve must carry it (pre-carve: config.py module body).
# Dropping it leaves the hook defined but never armed; test_gate_hermeticity_isolation is the guard.
sys.addaudithook(isolated_sqlite_connect_audit_hook)


def db_path():
    require_ledger_home_alignment(HOME)
    p = _cfg_get("budget", "db_path", None)
    return p if p else str(HOME / "spend.db")




# Nine pooled subsystems + one SpendLedger per worker, at dispatch.py's 24-worker
# default: 24 * 10 = 240, plus 16 coordinator/singleton/fresh connections = 256.
LEDGER_MAX_CONNECTIONS_DEFAULT = 256
LEDGER_READ_POOL_CAP_DEFAULT = 16
LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S = 60.0
LEDGER_CACHE_BUDGET_KIB = 256 * 1024
LEDGER_MMAP_BUDGET_BYTES = 512 * 1024 * 1024
# Connect/write/busy/WAL/read-busy/disconnect knobs measure independent operations by design.
LEDGER_CONNECT_TIMEOUT_S = 5
LEDGER_WRITE_TIMEOUT_S = 10
LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S = float(os.getenv(
    "SPENDGUARD_LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S", str(LEDGER_WRITE_TIMEOUT_S)))
# Poll deadlines every two thousand SQLite VM instructions, rather than every
# hundred: Python callbacks are especially expensive under coverage tracing.
# Wall-clock deadlines and transport interrupts remain authoritative; this is
# only the polling granularity, with bounded deadline/cancel guards below it.
LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS = 2000
LEDGER_READ_TIME_BUDGET_S = float(os.getenv("SPENDGUARD_LEDGER_READ_TIME_BUDGET_S", "5.0"))
LEDGER_QUERY_DEADLINE_S = float(os.getenv("SPENDGUARD_LEDGER_QUERY_DEADLINE_S", str(LEDGER_READ_TIME_BUDGET_S)))
LEDGER_READ_ROW_CAP = int(os.getenv("SPENDGUARD_LEDGER_READ_ROW_CAP", "10000"))
LEDGER_READ_PROGRESS_VM_INSTRUCTIONS = int(os.getenv(
    "SPENDGUARD_LEDGER_READ_PROGRESS_VM_INSTRUCTIONS", str(LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS)))
LEDGER_BUSY_TIMEOUT_MS = 5000
LEDGER_WAL_CHECKPOINT_INTERVAL_DEFAULT_S = 60.0
LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS = 100
LEDGER_WAL_CHECKPOINT_CADENCE_WAIT_FRACTION = 0.1
LEDGER_TIMEOUT_MS_PER_S = 1000
# SQLite's existing default, now explicit and owner-tunable without a source edit.
LEDGER_WAL_AUTOCHECKPOINT_PAGES = 1000
LEDGER_READ_BUSY_TIMEOUT_MS = 100
LEDGER_REQUEST_DISCONNECT_POLL_S = float(os.getenv("SPENDGUARD_LEDGER_REQUEST_DISCONNECT_POLL_S", "0.05"))
LEDGER_JOURNAL_LIMIT_BYTES = 64 * 1024 * 1024
LEDGER_HOME_IDENTITY_CACHE_SIZE = 1


class LedgerHomeIsolationError(BaseException):
    """A late home redirect cannot authorize writes to a stale real-home target.

    Deliberately escapes telemetry's best-effort Exception catches, including
    the dead-letter fallback which itself uses the import-time home.
    """


@lru_cache(maxsize=LEDGER_HOME_IDENTITY_CACHE_SIZE)
def _resolved_ledger_home_redirect(home, protected_default, requested_home, expansion_home, working_directory):
    """Cache one complete identity result, never a ledger-operation authorization.

    The last two inputs invalidate relative/tilde identities when their resolution
    context changes. Exceptions are not cached. The bounded cache is thread-safe;
    each caller uses its own returned tuple even during concurrent cache misses.
    Home symlink identities, like an open connection's identity, stay bound while
    these inputs remain unchanged.
    """
    cached_home = home.expanduser().resolve()
    if cached_home != protected_default.expanduser().resolve():
        return cached_home, False
    return cached_home, Path(requested_home).expanduser().resolve() != cached_home


@lru_cache(maxsize=LEDGER_HOME_IDENTITY_CACHE_SIZE)
def _ledger_home_spellings_are_absolute(home, protected_default, requested_home):
    """Cache only spelling facts; relative identities still depend on live cwd."""
    return os.path.isabs(home) and os.path.isabs(protected_default) and os.path.isabs(requested_home)


def require_ledger_home_alignment(path, *, opened_path_identity=None):
    """Refuse cached real-home files after an explicit environment redirect.

    Python startup hooks may import config before a test assigns its temporary
    home. Deliberate isolated-to-isolated redirects and unrelated explicitly
    selected databases remain usable; retaining the real home's target does not.
    """
    requested_home = os.environ.get(LEDGER_HOME_ENVIRONMENT_KEY)
    if not requested_home:
        return
    # Absolute spellings are independent of cwd. Avoid its filesystem syscall on
    # every managed cursor step, while relative/tilde spellings retain cwd in the
    # identity cache key so a chdir can never reuse stale authorization facts.
    absolute_spellings = _ledger_home_spellings_are_absolute(
        HOME, DEFAULT_SPENDGUARD_HOME, requested_home)
    working_directory = None if absolute_spellings else os.getcwd()
    expansion_home = (None if absolute_spellings
                      else os.environ.get(LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY))
    cached_home, redirected = _resolved_ledger_home_redirect(
        HOME, DEFAULT_SPENDGUARD_HOME, requested_home,
        expansion_home, working_directory)
    if not redirected:
        return
    # Managed handles retain the physical target selected at admission. Resolving
    # its spelling again on every fetch both traverses the filesystem and could
    # identify a different file after cwd/symlink changes, although SQLite still
    # holds the originally opened target. Direct callers resolve their live path.
    ledger_path = (opened_path_identity if opened_path_identity is not None
                   else Path(path).expanduser().resolve())
    if ledger_path == cached_home or cached_home in ledger_path.parents:
        raise LedgerHomeIsolationError(
            f"Ledger home changed after config import: {cached_home} -> {requested_home}; "
            f"refusing stale ledger {ledger_path}. Set {LEDGER_HOME_ENVIRONMENT_KEY} "
            "before interpreter startup or restart the interpreter.")


class _LedgerAdmissionGeneration:
    """Process-owned synchronization and exactly-once admission accounting."""

    def __init__(self):
        self.lock = threading.RLock()
        self.reclamation_lock = threading.RLock()
        self.owner_pid = os.getpid()
        self.abandoned_inherited_count = 0
        self.live_count = 0
        self.pending_count = 0
        self.active_ceiling = None


_LEDGER_ADMISSION = _LedgerAdmissionGeneration()


class LedgerConnectionLimitError(BaseException):
    """Process resource refusal, deliberately outside best-effort Exception catches.

    Telemetry readers routinely swallow database errors. Exhausting this hard
    control must propagate through those readers instead of becoming empty data.
    """


def ledger_connection_limit():
    """Read the live ceiling; invalid configuration fails closed."""
    value = _cfg_get("ledger", "max_connections_per_process", LEDGER_MAX_CONNECTIONS_DEFAULT)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("ledger.max_connections_per_process must be a positive integer")
    return value


def ledger_read_pool_settings():
    """Live read admission cap and legacy pool idle expiration, validated closed."""
    cap = int(os.getenv("SPENDGUARD_LEDGER_READ_POOL_CAP", _cfg_get(
        "ledger", "read_pool_cap", LEDGER_READ_POOL_CAP_DEFAULT)))
    idle = float(os.getenv("SPENDGUARD_LEDGER_POOL_IDLE_TIMEOUT_S", _cfg_get(
        "ledger", "pool_idle_timeout_s", LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S)))
    if cap <= 0 or not math.isfinite(idle) or idle <= 0:
        raise ValueError("ledger.read_pool_cap and ledger.pool_idle_timeout_s must be positive and finite")
    return cap, idle


def ledger_wal_checkpoint_interval_s():
    """Positive finite retry cadence for opportunistic WAL maintenance."""
    value = float(os.getenv("SPENDGUARD_LEDGER_WAL_CHECKPOINT_INTERVAL_S", _cfg_get(
        "ledger", "wal_checkpoint_interval_s", LEDGER_WAL_CHECKPOINT_INTERVAL_DEFAULT_S)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("ledger.wal_checkpoint_interval_s must be positive and finite")
    return value


def ledger_wal_checkpoint_busy_timeout_ms():
    """Bound lock waiting to a small fraction of the live maintenance cadence."""
    value = int(os.getenv("SPENDGUARD_LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", _cfg_get(
        "ledger", "wal_checkpoint_busy_timeout_ms", LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS)))
    ceiling = (ledger_wal_checkpoint_interval_s() * LEDGER_TIMEOUT_MS_PER_S
               * LEDGER_WAL_CHECKPOINT_CADENCE_WAIT_FRACTION)
    if value <= 0 or value > ceiling:
        raise ValueError("ledger.wal_checkpoint_busy_timeout_ms must be positive and at most one tenth of cadence")
    return value


def ledger_wal_autocheckpoint_pages():
    """Explicit automatic drain threshold; the default preserves SQLite behavior."""
    value = int(os.getenv("SPENDGUARD_LEDGER_WAL_AUTOCHECKPOINT_PAGES", _cfg_get(
        "ledger", "wal_autocheckpoint_pages", LEDGER_WAL_AUTOCHECKPOINT_PAGES)))
    if value <= 0:
        raise ValueError("ledger.wal_autocheckpoint_pages must be positive")
    return value


def ledger_read_busy_timeout_ms():
    """Brief explicit lock wait for managed committed-snapshot readers."""
    value = int(os.getenv("SPENDGUARD_LEDGER_READ_BUSY_TIMEOUT_MS", _cfg_get(
        "ledger", "read_busy_timeout_ms", LEDGER_READ_BUSY_TIMEOUT_MS)))
    if value <= 0:
        raise ValueError("ledger.read_busy_timeout_ms must be positive")
    return value


def ledger_connection_count():
    """Open managed connections in THIS process generation, including fresh ones.

    Pre-fork handles are abandoned and uncounted in a child by design; their
    count is retained as _LEDGER_ADMISSION.abandoned_inherited_count.
    """
    with _LEDGER_ADMISSION.lock:
        return _LEDGER_ADMISSION.live_count


def run_ledger_setup_pragmas(connection, pragmas):
    """Apply connection setup without entering managed-request cursor dispatch."""
    if isinstance(connection, _CountedLedgerConnection):
        # A home-aligned connection's own cursor dispatches through managed-request
        # cursor state; offline reconcile correction runs under a guard that forbids
        # that dispatch. Honor the home boundary explicitly, then run setup PRAGMAs
        # on a raw base cursor so connection setup never counts as a managed step.
        require_ledger_home_alignment(
            connection._ledger_home_path,
            opened_path_identity=connection._ledger_opened_path_identity,
        )
        with contextlib.closing(
                sqlite3.Connection.cursor(connection, sqlite3.Cursor)) as setup_cursor:
            for pragma in pragmas:
                sqlite3.Cursor.execute(setup_cursor, "PRAGMA " + pragma)
    else:
        for pragma in pragmas:
            connection.execute("PRAGMA " + pragma)


def budget_ledger_connection_memory(c, ceiling=None):
    """Share named process cache/mmap budgets across the configured ceiling."""
    ceiling = ledger_connection_limit() if ceiling is None else ceiling
    # ceiling * floor(budget / ceiling) <= budget: 256 MiB cache, 512 MiB mmap.
    pragmas = (f"cache_size={-(LEDGER_CACHE_BUDGET_KIB // ceiling)}",
               f"mmap_size={LEDGER_MMAP_BUDGET_BYTES // ceiling}")
    run_ledger_setup_pragmas(c, pragmas)


class _HomeAlignedLedgerCursor(sqlite3.Cursor):
    """Retained cursors enforce the same live home boundary as admission."""

    def _dispatch_home_aligned_cursor_operation(self, operation, *args, **kwargs):
        require_ledger_home_alignment(self.connection._ledger_home_path,
                                     opened_path_identity=self.connection._ledger_opened_path_identity)
        if getattr(self.connection, "_ledger_owned_read_deadline", False):
            return operation(self, *args, **kwargs)
        state = getattr(self, "_ledger_request_cursor_state", None)
        if state is None:
            state = self._ledger_request_cursor_state = make_managed_request_cursor_state(self)
        return state.dispatch_managed_request_cursor_step(operation, *args, **kwargs)

    def _release_request_cursor_if_idle(self):
        state = getattr(self, "_ledger_request_cursor_state", None)
        if state is not None:
            state.release_idle_managed_request_cursor()

    execute = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.execute)
    executemany = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.executemany)
    executescript = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.executescript)
    fetchone = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.fetchone)
    fetchmany = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.fetchmany)
    fetchall = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.fetchall)
    __next__ = partialmethod(_dispatch_home_aligned_cursor_operation, sqlite3.Cursor.__next__)


class _CountedLedgerConnection(sqlite3.Connection):
    def _dispatch_home_aligned_connection_operation(self, operation, *args, **kwargs):
        require_ledger_home_alignment(self._ledger_home_path,
                                     opened_path_identity=self._ledger_opened_path_identity)
        if operation is sqlite3.Connection.commit:
            if (probe_ledger_connection_commit_deferred(self)
                    or getattr(self, "_ledger_writer_transaction_aborted", False)):
                return None
            require_live_ledger_writer_request()
        return operation(self, *args, **kwargs)

    def _create_home_aligned_ledger_cursor(self, factory=_HomeAlignedLedgerCursor):
        require_ledger_home_alignment(self._ledger_home_path,
                                     opened_path_identity=self._ledger_opened_path_identity)
        if not issubclass(factory, _HomeAlignedLedgerCursor):
            raise LedgerHomeIsolationError("Ledger cursor factory must preserve the live home isolation guard")
        return sqlite3.Connection.cursor(self, factory)

    def _dispatch_home_aligned_cursor_shortcut(self, operation, *args, **kwargs):
        # SQLite's native connection shortcuts return an unguarded base Cursor.
        # Create our cursor explicitly so a caller retaining that result stays guarded.
        shortcut_cursor = sqlite3.Connection.cursor(self, _HomeAlignedLedgerCursor)
        try:
            return shortcut_cursor._dispatch_home_aligned_cursor_operation(operation, *args, **kwargs)
        except BaseException:
            shortcut_cursor.close()
            raise

    def set_progress_handler(self, progress_handler, n):
        self._ledger_progress = (progress_handler, n)
        return sqlite3.Connection.set_progress_handler(self, progress_handler, n)

    def set_authorizer(self, authorizer_callback):
        self._ledger_authorizer = authorizer_callback
        parser_facts = getattr(self, "_ledger_statement_parser_facts", None)
        if parser_facts is not None:
            with self._ledger_request_step_lock:
                return parser_facts.replace_managed_user_authorizer(authorizer_callback)
        return sqlite3.Connection.set_authorizer(self, authorizer_callback)

    cursor = partialmethod(_create_home_aligned_ledger_cursor)
    execute = partialmethod(_dispatch_home_aligned_cursor_shortcut, sqlite3.Cursor.execute)
    executemany = partialmethod(_dispatch_home_aligned_cursor_shortcut, sqlite3.Cursor.executemany)
    executescript = partialmethod(_dispatch_home_aligned_cursor_shortcut, sqlite3.Cursor.executescript)
    commit = partialmethod(_dispatch_home_aligned_connection_operation, sqlite3.Connection.commit)
    __exit__ = partialmethod(_dispatch_home_aligned_connection_operation, sqlite3.Connection.__exit__)

    def close(self):
        generation = getattr(self, "_ledger_generation", None)
        if generation is not _LEDGER_ADMISSION:
            return  # inherited handles never acquire an inherited lock
        # Idle expiry owns this lock through rollback AND callback restoration.
        # Closing between those steps invalidates its still-live native handle.
        with self._ledger_request_step_lock:
            super().close()
        with generation.lock:
            if not self._ledger_released:
                self._ledger_released = True
                generation.live_count -= 1

    def __del__(self, _close=sqlite3.Connection.close, _error=sqlite3.Error):
        generation = getattr(self, "_ledger_generation", None)
        if generation is None or generation is not _LEDGER_ADMISSION:
            return
        # Explicit close permanently releases this slot. Cyclic GC may finalize
        # the closed handle inside a deadline-bound reader while another thread
        # holds admission; there is no accounting left to synchronize here.
        if self._ledger_released:
            return
        try:
            _close(self)
        except _error:
            pass
        finally:
            with generation.lock:
                if not self._ledger_released:
                    self._ledger_released = True
                    generation.live_count -= 1


def open_ledger_connection(path=None, timeout=LEDGER_CONNECT_TIMEOUT_S, *, tune=True, deadline_at=None, **options):
    """Atomically admit, open and tune a connection; callers own its close."""
    deadline_at = resolve_ledger_admission_deadline(deadline_at)
    path = path or db_path()
    require_ledger_home_alignment(path)
    opened_path_identity = Path(path).expanduser().resolve()
    generation = _LEDGER_ADMISSION
    ceiling = ledger_connection_limit()
    if deadline_at is None:
        generation.lock.acquire()
    elif not generation.lock.acquire(timeout=max(0.0, deadline_at - time.monotonic())):
        resolve_ledger_admission_deadline(deadline_at)
        raise TimeoutError("Ledger connection admission deadline exceeded")
    try:
        current = generation.live_count + generation.pending_count
        if current >= ceiling or (current and ceiling != _LEDGER_ADMISSION.active_ceiling):
            # sqlite statements can form cycles. Reclaim unreachable connections
            # from expired workers before refusing; never evict a live reference.
            if deadline_at is None:
                generation.lock.release()
                try:
                    # gc.collect returns immediately if another thread is collecting.
                    # Join reclamation before treating the live count as a refusal.
                    # Finalizers need the admission lock; never hold it here.
                    # RLock permits synchronous GC callbacks to reenter admission.
                    with generation.reclamation_lock:
                        gc.collect()
                finally:
                    generation.lock.acquire()
            current = generation.live_count + generation.pending_count
        if current and ceiling != _LEDGER_ADMISSION.active_ceiling:
            raise LedgerConnectionLimitError(
                f"Ledger connection ceiling cannot change from {_LEDGER_ADMISSION.active_ceiling} "
                f"to {ceiling} with current live count {current}; close connections first")
        if current >= ceiling:
            raise LedgerConnectionLimitError(
                f"Ledger connection ceiling {ceiling} reached: current live count {current}")
        generation.pending_count += 1
        generation.active_ceiling = ceiling
    finally:
        generation.lock.release()
    reservation_pending = True
    try:
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Ledger connection setup deadline exceeded")
            timeout = min(timeout, remaining)
        options.setdefault("check_same_thread", False)
        c = sqlite3.connect(path, timeout=timeout,
                            factory=_CountedLedgerConnection, **options)
        from spendguard.content_compression import ledger_text_factory
        c.text_factory = ledger_text_factory
        c._ledger_home_path = path
        c._ledger_opened_path_identity = opened_path_identity
        c._ledger_request_step_lock = threading.RLock()
        c._ledger_generation = generation
        c._ledger_released = False
        with generation.lock:
            generation.pending_count -= 1
            reservation_pending = False
            generation.live_count += 1
        try:
            if tune:
                tune_ledger_connection(c, ceiling=ceiling)
            else:
                budget_ledger_connection_memory(c, ceiling=ceiling)
        except BaseException:
            c.close()
            raise
        return c
    finally:
        if reservation_pending:
            with generation.lock:
                generation.pending_count -= 1


_LEDGER_SCHEMA_LOCK = threading.RLock()
_LEDGER_JOURNAL_MODE_LOCK = threading.RLock()


def ensure_ledger_schema(c, schema):
    """Run complete idempotent setup on every open, including same-file DDL repair.

    File identity cannot prove tables, additive columns or indexes still exist.
    Serialize local migrations; callbacks retain their cross-process race safety.
    """
    database_path = c.execute("PRAGMA database_list").fetchone()[2]
    owner_connection = resolve_active_ledger_writer_connection(database_path) if database_path else None
    native_connection = getattr(c, "_native_reconcile_connection", c)
    if database_path and owner_connection is not native_connection:
        return run_wired_ledger_write(lambda: ensure_ledger_schema(
            resolve_active_ledger_writer_connection(database_path), schema), database_path)
    with acquire_ledger_writer_deadline_lock(_LEDGER_SCHEMA_LOCK):
        preserve_transaction = native_connection.in_transaction or probe_ledger_connection_commit_deferred(native_connection)
        if owner_connection is native_connection:
            with enter_isolated_nested_ledger_write(native_connection, sql_connection=c):
                result = schema(c)
        else:
            result = schema(c)
        if not preserve_transaction:
            c.commit()
        return result


def tune_ledger_connection(c, ceiling=None):
    """Apply spendguard's shared SQLite tuning to a base-ledger connection. config.db_path() is ONE file opened by
    ~8 subsystems (budget/calls/callio/bulkgate/learn/semcache/lane_queue …); every `_X_db()` opener calls this right
    after connect(), replacing a bare `PRAGMA journal_mode=WAL`, so the whole shared ledger takes one tuned posture:
      · journal_mode=WAL   — concurrent readers + one writer (persistent; asserted so a first-ever open sets it).
      · synchronous=NORMAL — under WAL, DON'T fsync on every commit (the dominant per-write cost). Durable vs an APP
        crash; only an OS/power crash can lose commits since the last WAL checkpoint, and spendguard's reconcile
        (provider billing = ground truth) is the backstop for the spend ledger.
      · busy_timeout=5000  — wait up to 5s for the single shared-file writer lock, then fail; callers degrade a busy
        write, never crash.
      · temp_store=MEMORY  — sorts / temp b-trees in RAM.
      · cache_size/mmap_size — process budgets divided by the configured connection ceiling.
      · journal_size_limit=64MB — after a checkpoint, TRUNCATE the -wal file back to this instead of leaving it at its
        high-water mark. Without it a passive autocheckpoint resets the WAL logically but never shrinks the FILE, so a
        burst of large write txns (the lane-drain's purge used to be one every cycle) left a multi-GB -wal on disk
        forever: measured spend.db-wal at 1.3GB, 2026-10-08. This caps it; a periodic wal_checkpoint(TRUNCATE) drains it.
    All per-CONNECTION except WAL + journal_size_limit (file-persistent), so tuning one subsystem's connection never
    disturbs another's on the same file. Required tuning failures propagate; an untuned owner cannot serve writes."""
    with acquire_ledger_writer_deadline_lock(_LEDGER_JOURNAL_MODE_LOCK):
        with contextlib.closing(c.execute("PRAGMA journal_mode")) as journal_cursor:
            journal_mode = journal_cursor.fetchone()[0]
        if journal_mode.lower() != "wal":
            with contextlib.closing(c.execute("PRAGMA journal_mode=WAL")) as journal_cursor:
                journal_mode = journal_cursor.fetchone()[0]
    for pragma in (f"busy_timeout={LEDGER_BUSY_TIMEOUT_MS}", "synchronous=NORMAL", "temp_store=MEMORY",
                   f"journal_size_limit={LEDGER_JOURNAL_LIMIT_BYTES}",
                   f"wal_autocheckpoint={ledger_wal_autocheckpoint_pages()}"):
        c.execute("PRAGMA " + pragma).close()
    budget_ledger_connection_memory(c, ceiling=ceiling)
    return c


# ── shared ledger connection POOL — ONE implementation for every subsystem that opens the base sqlite ───────────────
# config.db_path() is a SINGLE file opened by ~8 subsystems (budget/calls/callio/bulkgate/learn/semcache/lane_queue…).
# Each used to reopen + re-tune + re-check its schema on EVERY op — and the connect() alone (~400us) DOMINATED the
# per-op cost (measured ~60x on a write hot path; PRAGMAs per-op are neutral, reuse is the lever). This is the ONE
# reuse+tuning implementation they all share: a THREAD-LOCAL connection per (thread, subsystem `key`), tuned once,
# schema ensured once, self-healing on error. KEYED per subsystem (NOT one globally shared connection) so a nested op
# across subsystems can never commit/rollback another's in-flight transaction — the spend ledger must not be coupled
# to the queue's transaction. This replaces the copy-pasted per-op `_X_db()` open in every subsystem.
_LEDGER_POOL = threading.local()
_LEDGER_POOL_REGISTRY = weakref.WeakSet()
_LEDGER_POOL_REGISTRY_LOCK = threading.RLock()
_LEDGER_READ_POOL_CONDITION = threading.Condition(_LEDGER_POOL_REGISTRY_LOCK)
_LEDGER_READ_POOL_ACTIVE = 0


def recycle_idle_ledger_connections(*, expired_only=False):
    """Close tracked idle legacy handles across threads; never interrupt a transaction."""
    _, idle_timeout = ledger_read_pool_settings()
    reclaimed = 0
    now = time.monotonic()
    with acquire_ledger_writer_deadline_lock(_LEDGER_POOL_REGISTRY_LOCK):
        for connection in tuple(_LEDGER_POOL_REGISTRY):
            if expired_only and now - connection._ledger_pool_last_used < idle_timeout:
                continue
            if not connection._ledger_request_step_lock.acquire(blocking=False):
                continue
            try:
                if connection._ledger_released:
                    _LEDGER_POOL_REGISTRY.discard(connection)
                elif not connection.in_transaction and not getattr(connection, "_ledger_operation_borrowed", False):
                    connection.close()
                    _LEDGER_POOL_REGISTRY.discard(connection)
                    reclaimed += 1
            finally:
                connection._ledger_request_step_lock.release()
    return reclaimed


@contextlib.contextmanager
def managed_ledger_read_connection(path, *, timeout=LEDGER_CONNECT_TIMEOUT_S, deadline_at=None, **options):
    """Hard capped read leases, with physical close on every return and exception.

    No returned read handle is retained idle: idle capacity is always zero. The
    legacy pool uses the shared configured idle timeout for its remaining users.
    """
    global _LEDGER_READ_POOL_ACTIVE
    deadline_at = resolve_ledger_admission_deadline(deadline_at)
    cap, idle_timeout = ledger_read_pool_settings()
    with acquire_ledger_writer_deadline_lock(_LEDGER_READ_POOL_CONDITION, deadline_at=deadline_at):
        while True:
            deadline_at = resolve_ledger_admission_deadline(deadline_at)
            pooled_live = sum(not handle._ledger_released for handle in _LEDGER_POOL_REGISTRY)
            if _LEDGER_READ_POOL_ACTIVE + pooled_live >= cap:
                recycle_idle_ledger_connections()
                pooled_live = sum(not handle._ledger_released for handle in _LEDGER_POOL_REGISTRY)
            if _LEDGER_READ_POOL_ACTIVE + pooled_live < cap:
                break
            remaining = None if deadline_at is None else deadline_at - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Ledger read pool admission deadline exceeded")
            _LEDGER_READ_POOL_CONDITION.wait(idle_timeout if remaining is None else min(remaining, idle_timeout))
        _LEDGER_READ_POOL_ACTIVE += 1
    try:
        with contextlib.closing(open_ledger_connection(
                path, timeout=timeout, tune=False, deadline_at=deadline_at, **options)) as connection:
            run_ledger_setup_pragmas(connection, (f"busy_timeout={ledger_read_busy_timeout_ms()}",))
            yield connection
    finally:
        with _LEDGER_READ_POOL_CONDITION:
            _LEDGER_READ_POOL_ACTIVE -= 1
            _LEDGER_READ_POOL_CONDITION.notify()


def open_ledger_maintenance_connection(path=None, *, read_only=True, timeout=0):
    """Unpooled maintenance handle; inspection never sets persistent pragmas.

    Normal tune_ledger_connection sets WAL and tolerates pragma failures. Offline
    maintenance must instead control locks explicitly and fail closed.
    """
    import sqlite3
    ledger = Path(path or db_path()).resolve(strict=True)
    mode = "ro" if read_only else "rw"
    connection = sqlite3.connect(f"{ledger.as_uri()}?mode={mode}", uri=True, timeout=timeout,
                                 isolation_level=None)
    from spendguard.content_compression import ledger_text_factory
    connection.text_factory = ledger_text_factory
    return connection


def pooled_ledger_conn(key, ensure_schema, path=None):
    """The thread-local pooled connection for subsystem `key` (tuned via tune_ledger_connection, schema fully ensured
    on every new connection). Reused across that subsystem's ops on this thread; reopened when the path
    changed under us (a test) or the connection was dropped by reset_ledger_conn after an error, so a stale connection
    never lingers. `key` namespaces one subsystem's connection from another's. `path` defaults to the shared ledger
    (db_path()); a subsystem on its OWN file (e.g. lane_queue → lane_queue_db_path()) passes it, and the pool keys the
    connection by its path too, so the same key never returns a connection to the wrong file."""
    global _LEDGER_READ_POOL_ACTIVE
    resolve_ledger_admission_deadline()
    writer_connection = resolve_active_ledger_writer_connection(path)
    if writer_connection is not None:
        schema_keys = writer_connection._ledger_writer_schema_keys
        if key not in schema_keys:
            ensure_ledger_schema(writer_connection, ensure_schema)
            schema_keys.add(key)
        return writer_connection
    conns = getattr(_LEDGER_POOL, "conns", None)
    if conns is None:
        conns = _LEDGER_POOL.conns = {}
    path = path or db_path()
    require_ledger_home_alignment(path)
    got = conns.get(key)
    if got is not None and got[1] == path and not getattr(got[0], "_ledger_released", False):
        got[0]._ledger_pool_last_used = time.monotonic()
        got[0]._ledger_operation_borrowed = True
        return got[0]
    if got is not None:                                  # db path changed under us → close the stale connection first
        got[0].close()
    with acquire_ledger_writer_deadline_lock(_LEDGER_POOL_REGISTRY_LOCK):
        recycle_idle_ledger_connections(expired_only=True)
        cap, admission_timeout = ledger_read_pool_settings()
        admission_deadline = resolve_ledger_admission_deadline(time.monotonic() + admission_timeout)
        while True:
            resolve_ledger_admission_deadline(admission_deadline)
            live = sum(not handle._ledger_released for handle in _LEDGER_POOL_REGISTRY)
            if live + _LEDGER_READ_POOL_ACTIVE < cap:
                break
            recycle_idle_ledger_connections()
            live = sum(not handle._ledger_released for handle in _LEDGER_POOL_REGISTRY)
            if live + _LEDGER_READ_POOL_ACTIVE < cap:
                break
            remaining = admission_deadline - time.monotonic()
            if remaining <= 0:
                raise LedgerConnectionLimitError(f"Ledger read pool ceiling {cap} reached")
            _LEDGER_READ_POOL_CONDITION.wait(timeout=remaining)
        _LEDGER_READ_POOL_ACTIVE += 1
    try:
        c = fresh_ledger_conn(ensure_schema, path)
        c._ledger_pool_last_used = time.monotonic()
        c._ledger_operation_borrowed = True
        try:
            with acquire_ledger_writer_deadline_lock(_LEDGER_POOL_REGISTRY_LOCK):
                _LEDGER_POOL_REGISTRY.add(c)
        except BaseException:
            c.close()
            raise
    finally:
        with _LEDGER_READ_POOL_CONDITION:
            _LEDGER_READ_POOL_ACTIVE -= 1
            _LEDGER_READ_POOL_CONDITION.notify_all()
    conns[key] = (c, path)
    from spendguard.ledger_wal_maintenance import maintain_ledger_wal_if_due
    try:
        maintain_ledger_wal_if_due(path)
    except BaseException:
        reset_ledger_conn(key)
        raise
    return c


def rollback_ledger_conn(key):
    """Abort the active writer, otherwise roll back subsystem `key`'s legacy pool handle.

    Call it from a write's error path so a
    failed DML whose exception the caller SWALLOWS can't leave an open transaction holding the WAL write lock on the
    reused connection until this thread's next write. Best-effort + never raises; a no-op when there's nothing open or
    no connection yet. Writer rollback failures propagate: an owner that cannot abort must never commit.
    Unlike reset_ledger_conn it keeps the connection; only its aborted transaction needs clearing."""
    if abort_active_ledger_writer_transaction():
        return
    conns = getattr(_LEDGER_POOL, "conns", None)
    if not conns:
        return
    got = conns.get(key)
    if got is not None:
        try:
            got[0].rollback()
        except Exception:
            pass


def reset_ledger_conn(key):
    """Abort/reset the active writer, otherwise drop subsystem `key`'s legacy handle.

    The writer closes after the active callback/bulk scope unwinds and reopens at next admission;
    no borrowed handle is closed underneath executing code.
    """
    if abort_active_ledger_writer_transaction(reset=True):
        return
    conns = getattr(_LEDGER_POOL, "conns", None)
    if not conns:
        return
    got = conns.pop(key, None)
    if got is not None:
        got[0].close()
        with _LEDGER_READ_POOL_CONDITION:
            _LEDGER_READ_POOL_CONDITION.notify_all()


@contextlib.contextmanager
def ledger_op(key, ensure_schema, path=None):
    """Own one operation: commit on success, rollback on failure, CLOSE on return.

    Caller-held references cannot keep an operation's SQLite descriptor alive.
    Every later operation obtains a new lease, including after BaseException.
    """
    while True:
        c = pooled_ledger_conn(key, ensure_schema, path=path)
        with acquire_ledger_writer_deadline_lock(_LEDGER_POOL_REGISTRY_LOCK):
            if c._ledger_released:
                continue
            c._ledger_operation_borrowed = True
            c._ledger_operation_scope_depth = getattr(c, "_ledger_operation_scope_depth", 0) + 1
            break
    writer_owned = c is resolve_active_ledger_writer_connection(path)
    try:
        yield c
        if c._ledger_operation_scope_depth == 1 and not probe_ledger_connection_commit_deferred(c):
            c.commit()
        if not writer_owned and c._ledger_operation_scope_depth == 1:
            from spendguard.ledger_wal_maintenance import maintain_ledger_wal_if_due
            maintain_ledger_wal_if_due(path or db_path())
    except BaseException:
        try:
            c.rollback()
        except Exception:
            pass
        if not writer_owned and c._ledger_operation_scope_depth == 1:
            reset_ledger_conn(key)
        raise
    finally:
        c._ledger_operation_scope_depth -= 1
        c._ledger_operation_borrowed = c._ledger_operation_scope_depth > 0
        if not writer_owned and not c._ledger_operation_borrowed:
            reset_ledger_conn(key)


def with_ledger_read_lease(pool_key, schema_fn, *, path_provider=None):
    """Scope an existing read entrypoint and its nested getters to one closeable lease."""
    from functools import wraps

    def decorate_ledger_read_entrypoint(function):
        @wraps(function)
        def invoke_ledger_read_entrypoint(*args, **kwargs):
            path = path_provider() if path_provider is not None else None
            with ledger_op(pool_key, schema_fn, path):
                return function(*args, **kwargs)
        return invoke_ledger_read_entrypoint
    return decorate_ledger_read_entrypoint


def fresh_ledger_conn(ensure_schema, path=None):
    """A FRESH, closeable connection to the ledger — for occasional EXTERNAL/one-off use (a test's manual setup, a CLI
    inspection) opened `with contextlib.closing(...)` and CLOSED. Deliberately NOT the pooled connection, so closing it
    can never corrupt the pool. `path` selects the file (default: the shared ledger, db_path())."""
    path = path or db_path()
    c = open_ledger_connection(path)
    try:
        ensure_ledger_schema(c, ensure_schema)
    except BaseException:
        c.close()
        raise
    return c


def _abandon_inherited_ledger_generation_in_fork_child():
    """A forked child must NOT reuse the parent's pooled connections — the sqlite fds and their POSIX locks belong to
    the parent, so a child that inherits them gets 'database is locked' or silent corruption. Clear this thread's pool
    (a fork keeps only the forking thread) WITHOUT closing — closing a fd the parent still owns would break the parent;
    the child reopens fresh on next use. This register_at_fork(after_in_child) callback
    only transitions a NEW child: owner-PID equality makes same-process calls no-ops.
    The new generation exposes abandoned_inherited_count without taking the inherited lock."""
    global _LEDGER_ADMISSION, _LEDGER_SCHEMA_LOCK, _LEDGER_JOURNAL_MODE_LOCK, _LEDGER_POOL_REGISTRY
    global _LEDGER_POOL_REGISTRY_LOCK, _LEDGER_READ_POOL_CONDITION, _LEDGER_READ_POOL_ACTIVE
    if _LEDGER_ADMISSION.owner_pid == os.getpid():
        return  # Same-process calls cannot reset live admission slots.
    # POSIX after_in_child is single-threaded; no parent execution context survives.
    # Admission reads the current lock at entry, never a cached lock alias.
    # Old connections retain their generation and cannot release child slots.
    inherited_generation = _LEDGER_ADMISSION
    _LEDGER_ADMISSION = _LedgerAdmissionGeneration()
    _LEDGER_SCHEMA_LOCK = threading.RLock()
    _LEDGER_JOURNAL_MODE_LOCK = threading.RLock()
    _LEDGER_POOL_REGISTRY = weakref.WeakSet()
    _LEDGER_POOL_REGISTRY_LOCK = threading.RLock()
    _LEDGER_READ_POOL_CONDITION = threading.Condition(_LEDGER_POOL_REGISTRY_LOCK)
    _LEDGER_READ_POOL_ACTIVE = 0
    _LEDGER_ADMISSION.abandoned_inherited_count = inherited_generation.live_count
    try:
        _LEDGER_POOL.conns = {}
    except Exception:
        pass


try:
    import os as _os_at_fork
    _os_at_fork.register_at_fork(after_in_child=_abandon_inherited_ledger_generation_in_fork_child)
except (AttributeError, ValueError, ImportError):   # register_at_fork is POSIX-only — elsewhere a no-op
    pass
