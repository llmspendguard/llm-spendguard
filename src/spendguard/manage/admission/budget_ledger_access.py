"""Shared ledger-access plumbing for budget admission and spend reads."""
import contextlib
import functools
import hashlib
import inspect
import math
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
from decimal import Decimal
from numbers import Number

from ... import config
from ...ledger_fork_locks import new_fork_reset_ledger_state
from .admission_accessors import BUDGET_AUXILIARY_POOL_KEY, UNATTRIBUTED_PROJECT


def require_finite_budget_value(value):
    """Reject incomplete monetary arithmetic instead of manufacturing a cap."""
    finite = value.is_finite() if isinstance(value, Decimal) else (
        math.isfinite(value) if isinstance(value, Number) else True)
    if not finite:
        raise ValueError("Budget projection requires finite monetary values")
    if isinstance(value, (tuple, list)):
        for component in value:
            require_finite_budget_value(component)
    return value


def finite_budget_projection(project):
    @functools.wraps(project)
    def enforce_finite_budget_projection(*args, **kwargs):
        for value in (*args, *kwargs.values()):
            require_finite_budget_value(value)
        return require_finite_budget_value(project(*args, **kwargs))
    return enforce_finite_budget_projection


def _budget_snapshot_generation_observer():
    """Keep SQLite's connection-relative commit counter on a read-only owner."""
    from spendguard.store.spend_search.ledger_read_accessors import resolve__consumer_ledger_read_target as _consumer_ledger_read_target, resolve_effective_ledger_admission_deadline as effective_ledger_admission_deadline
    path = pathlib.Path(config.db_path()).resolve()
    read_target = _consumer_ledger_read_target(path)
    # Empty-install compatibility owns a transient database until the real ledger
    # materializes. Its commit counter cannot observe that different database.
    observer_identity = (path, read_target)
    observer = getattr(_LEDGER_TL, "projection_observer", None)
    if observer is not None and observer[0] != observer_identity:
        observer[1].__exit__(None, None, None)
        _LEDGER_TL.projection_observer = None
        observer = None
    if observer is None:
        owner = contextlib.closing(config.open_ledger_connection(read_target, uri=True, tune=False,
            deadline_at=effective_ledger_admission_deadline(
                time.monotonic() + config.LEDGER_QUERY_DEADLINE_S)))
        connection = owner.__enter__()
        observer = (observer_identity, owner, connection)
        _LEDGER_TL.projection_observer = observer
    try:
        return observer[2], observer[2].execute("PRAGMA data_version").fetchone()[0]
    except BaseException:
        _LEDGER_TL.projection_observer = None
        observer[1].__exit__(*sys.exc_info())
        raise


@contextlib.contextmanager
def budget_projection_reader(*, deadline_s=None, rollup_eligible=True):
    """Use committed WAL snapshots independently of pending writer work."""
    from ...ledger import SpendLedger
    from spendguard.store.spend_search.ledger_read_contracts import SPEND_EVENT_READ_COLUMNS, LedgerReadTimeout, LedgerReadError, LedgerReadOpenError, LedgerRequestAbandoned, LedgerSettlementIncomplete, LedgerQueryDeadline, LEDGER_READ_SQLITE_PRIMARY_CODE_MASK
    from spendguard.store.spend_search.ledger_read_accessors import resolve_default_ledger_read_time_budget_s
    from spendguard.store.spend_search.ledger_read_accessors import resolve__ledger_read_connection as _ledger_read_connection, resolve_consumer_empty_spend_compatibility as consumer_empty_spend_compatibility, resolve_effective_ledger_admission_deadline as effective_ledger_admission_deadline
    time_budget = resolve_default_ledger_read_time_budget_s() if deadline_s is None else deadline_s
    if not math.isfinite(time_budget) or time_budget <= 0:
        raise LedgerQueryDeadline(time_budget)
    projection_deadline = time.monotonic() + time_budget
    existing = getattr(_LEDGER_TL, "read_ledger", None)
    if existing is not None:
        yield existing
        return
    cached = getattr(_LEDGER_TL, "led", None)
    try:
        with consumer_empty_spend_compatibility(), _ledger_read_connection(
                time_budget=time_budget) as connection:
            snapshot_reader = object.__new__(SpendLedger)
            snapshot_reader.db_path = config.db_path()
            snapshot_reader._conn = connection
            snapshot_reader._cols = set(SPEND_EVENT_READ_COLUMNS)
            snapshot_reader._explicit_owned_connection = True
            snapshot_reader._total_memo = None
            snapshot_reader._budget_projection_connection_owned = True
            snapshot_reader._budget_projection_snapshot = True
            snapshot_reader._budget_projection_generation_observer = _budget_snapshot_generation_observer
            # Capture the observer before anything can pin the snapshot. Later
            # concurrent commits make this snapshot cache-ineligible.
            if rollup_eligible:
                snapshot_reader._daily_rollup_snapshot_generation = None
                try:
                    snapshot_reader._daily_rollup_snapshot_generation = _budget_snapshot_generation_observer()
                except (TimeoutError, LedgerReadTimeout):
                    if time.monotonic() >= effective_ledger_admission_deadline(projection_deadline):
                        raise LedgerQueryDeadline(time_budget)
                except sqlite3.Error as failure:
                    if (getattr(failure, "sqlite_errorcode", 0) & LEDGER_READ_SQLITE_PRIMARY_CODE_MASK
                            not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT)):
                        raise
                    if time.monotonic() >= effective_ledger_admission_deadline(projection_deadline):
                        raise LedgerQueryDeadline(time_budget) from failure
            # Coverage and money observe one complete committed version.
            connection.execute("BEGIN")
            snapshot_reader._total_memo = getattr(cached, "_total_memo", None)
            yield snapshot_reader
            if cached is not None:
                cached._total_memo = snapshot_reader._total_memo
    except (LedgerReadOpenError, LedgerRequestAbandoned, LedgerSettlementIncomplete):
        raise
    except (LedgerReadError, TimeoutError) as failure:
        raise LedgerQueryDeadline(time_budget) from failure


def budget_projection_snapshot(read_projection):
    projection_signature = inspect.signature(read_projection)

    @functools.wraps(read_projection)
    def read_budget_projection_snapshot(*args, **kwargs):
        arguments = projection_signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        rollup_eligible = not any(arguments.arguments.get(scope) is not None
                                  for scope in ("project", "conv", "job"))
        with budget_projection_reader(deadline_s=arguments.arguments.get("deadline_s"),
                                      rollup_eligible=rollup_eligible) as reader:
            previous = getattr(_LEDGER_TL, "read_ledger", None)
            _LEDGER_TL.read_ledger = reader
            try:
                if (getattr(reader, "_budget_projection_snapshot", False)
                        and "deadline_s" in projection_signature.parameters):
                    arguments.arguments["deadline_s"] = None
                    args, kwargs = arguments.args, arguments.kwargs
                return require_finite_budget_value(read_projection(*args, **kwargs))
            finally:
                _LEDGER_TL.read_ledger = previous
    return read_budget_projection_snapshot


_conn = None
_lock = new_fork_reset_ledger_state(globals(), "_lock", threading.RLock)
_SPEND_EVENT_DEADLETTER_FILENAME = "spend_events_deadletter.jsonl"
_SPEND_EVENT_EMERGENCY_SPOOL_PREFIX = "spendguard-capture-gap-"
# Visible attribution fallback for events with no explicit project or captured venv.
LIVE_GATE_SOURCE = "gate"


def spend_event_retry_paths():
    """Ordered durable retry spools; the OS temp fallback survives an unwritable spendguard home."""
    home_key = hashlib.sha256(str(config.HOME).encode()).hexdigest()
    return (config.HOME / _SPEND_EVENT_DEADLETTER_FILENAME,
            pathlib.Path(tempfile.gettempdir()) /
            f"{_SPEND_EVENT_EMERGENCY_SPOOL_PREFIX}{home_key}.jsonl")


def _ensure_budget_auxiliary_read_schema(connection):
    """Auxiliary tables are initialized by their serialized writer owners."""


def _ledger_db():
    """Borrow the writer owner inside mutations, or acquire a bounded per-thread auxiliary read handle.
    It NO LONGER creates the retired `charges` table: the money-of-record is `spend_events` (owned by
    SpendLedger), every reader/writer here goes through it, and a fresh install has no `charges` to make.
    The one-time `migrate_charges` bridge reads a PRE-EXISTING charges via its own connection; it never needs
    this to create one, and the migration tests build their own charges fixture."""
    from ...ledger_writer import current_ledger_writer_connection
    owner = current_ledger_writer_connection()
    if owner is not None:
        return owner
    return config.pooled_ledger_conn(BUDGET_AUXILIARY_POOL_KEY, _ensure_budget_auxiliary_read_schema)


_PROJECT = None


# The ONE definition of "money spent in a period" now lives on the money-of-record as SpendLedger._COUNTABLE
# (exclude meta / reconciliation markers / quarantined-impossible / reconstructed; an estimate still binds a
# cap). It replaced the countable_charges VIEW over the retired `charges` table — same rule, one place.


_LEDGER_TL = threading.local()


def _ledger():
    """Cache a connection-free facade; reads close scoped handles and mutations borrow the serialized owner."""
    scoped_reader = getattr(_LEDGER_TL, "read_ledger", None)
    if scoped_reader is not None:
        return scoped_reader
    led = getattr(_LEDGER_TL, "led", None)
    if led is None:
        from ...ledger import SpendLedger
        led = _LEDGER_TL.led = SpendLedger()
    from ...ledger_writer import current_ledger_writer_connection
    owner_connection = current_ledger_writer_connection()
    if owner_connection is not None:
        import copy
        led = copy.copy(led)
        led._conn = owner_connection
    return led


def _budget_writer_is_initialized():
    """An existing facade or executing owner can read without schema initialization."""
    from ...ledger_writer import current_ledger_writer_connection
    if current_ledger_writer_connection(config.db_path()) is not None:
        return True
    initialized_ledger = getattr(_LEDGER_TL, "led", None)
    return (initialized_ledger is not None
            and pathlib.Path(initialized_ledger.db_path).resolve() == pathlib.Path(config.db_path()).resolve())


def _consumer_aggregate_snapshot(aggregate):
    """Keep pure consumer aggregates off schema initialization and writer admission."""
    deadline_parameter = inspect.signature(aggregate).parameters.get("deadline_s")

    @functools.wraps(aggregate)
    def invoke_consumer_budget_aggregate(*args, **kwargs):
        if (_budget_writer_is_initialized()
                or getattr(_LEDGER_TL, "read_ledger", None) is not None):
            return aggregate(*args, **kwargs)
        from ...ledger import SpendLedger
        from spendguard.store.spend_search.ledger_read_contracts import SPEND_EVENT_READ_COLUMNS, LedgerReadTimeout, LedgerQueryDeadline
        from spendguard.store.spend_search.ledger_read_accessors import resolve_default_ledger_read_time_budget_s
        from spendguard.store.spend_search.ledger_read_accessors import resolve__ledger_read_connection as _ledger_read_connection, resolve_consumer_empty_spend_compatibility as consumer_empty_spend_compatibility
        aggregate_budget = (kwargs.get("deadline_s", deadline_parameter.default)
                            if deadline_parameter is not None else resolve_default_ledger_read_time_budget_s())
        if aggregate_budget is None:
            aggregate_budget = resolve_default_ledger_read_time_budget_s()
        try:
            with consumer_empty_spend_compatibility(), \
                    _ledger_read_connection(time_budget=aggregate_budget) as aggregate_connection:
                aggregate_reader = object.__new__(SpendLedger)
                aggregate_reader.db_path = config.db_path()
                aggregate_reader._conn = aggregate_connection
                aggregate_reader._cols = set(SPEND_EVENT_READ_COLUMNS)
                aggregate_reader._explicit_owned_connection = True
                aggregate_reader._total_memo = None
                aggregate_reader._defer = False
                _LEDGER_TL.read_ledger = aggregate_reader
                try:
                    return aggregate(*args, **kwargs)
                finally:
                    _LEDGER_TL.read_ledger = None
        except LedgerReadTimeout as aggregate_timeout:
            raise LedgerQueryDeadline(aggregate_budget) from aggregate_timeout
    return invoke_consumer_budget_aggregate


# Expose the read-connection aggregate decorator through the admission seam so a carved reader sibling can apply it
# at def-time (@_admission_accessors.resolve__consumer_aggregate_snapshot) WITHOUT importing budget. The decorator
# STAYS here, coupled to budget's _LEDGER_TL read state and _budget_writer_is_initialized; only the ability to apply
# it crosses the seam. Wired EARLY — before any reader-cluster re-import later in this module — so the resolver is
# already live when a sibling is imported and decorates one of its readers with it (the resolve_(fn) protocol doubles
# as a decorator because applying @D to `def f` is just `f = D(f)`, and functools.wraps is preserved through it).
from . import admission_accessors as _admission_accessors
_admission_accessors.set__consumer_aggregate_snapshot_resolver(
    lambda aggregate: _consumer_aggregate_snapshot(aggregate))


def _reset_ledger():
    """Drop THIS thread's cached ledger connection so the next read reconnects clean — call after a destructive
    schema change. Replaces the old `budget._LEDGER = None`, which a thread-local cache cannot honor."""
    _LEDGER_TL.led = None
    _LEDGER_TL.read_ledger = None
    observer = getattr(_LEDGER_TL, "projection_observer", None)
    if observer is not None:
        observer[1].__exit__(None, None, None)
        _LEDGER_TL.projection_observer = None


def _reset_budget_after_fork():
    """A child of os.fork() must NOT reuse the parent's sqlite connections. sqlite forbids sharing a connection
    across processes — the fd and its POSIX locks belong to the parent — so a child that inherits `_conn` (the
    shared module connection) or the parent's thread-local ledger gets `database is locked`, silent corruption, or
    a double-commit of the parent's pending write. Drop both so the child reconnects fresh on next use. Registered
    at fork below; a no-op on platforms without os.register_at_fork."""
    global _conn
    _conn = None
    try:
        _reset_ledger()
    except Exception:
        pass


try:
    import os as _os
    _os.register_at_fork(after_in_child=_reset_budget_after_fork)
except (AttributeError, ValueError, ImportError):   # register_at_fork is POSIX-only — elsewhere this is a no-op
    pass


def normalize_ledger_project_id(project):
    """Use the stored project identifier's orthographic convention for reads and writes."""
    return str(project).strip().lower() if project is not None else ""


def _attribute(ev, project):
    """Stamp org/team/project on a spend_event from the charge's project tag via the prior repo→org map — a
    cheap cached dict lookup, NOT an LLM (agentic refinement is the later attribute/reconcile pass, never the
    hot path). Keeps the same attribution the charges ledger carried, in the typed columns."""
    proj = normalize_ledger_project_id(project)
    if not proj:
        proj = UNATTRIBUTED_PROJECT
    try:
        from ... import conv as _conv_mod
        org, team = _conv_mod._prior_org_team(proj) if proj else ("", "")
    except Exception:
        org, team = "", ""
    ev["project_primary"] = proj
    ev["projects"] = [proj] if proj else []
    ev["org"], ev["team"] = org, team


def _project():
    """Project tag for a charge (the repo/work this spend belongs to) — cached per process. Order:
    $SPENDGUARD_PROJECT → saas config `project` (repo-local .spendguard.json) → git repo root basename → cwd."""
    global _PROJECT
    if _PROJECT is not None:
        return _PROJECT
    import os
    v = os.environ.get("SPENDGUARD_PROJECT")
    if not v:
        try:
            v = config.saas_config().get("project")
        except Exception:
            v = None
    if not v:
        try:
            import subprocess
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2).stdout.strip()
            if root:
                v = os.path.basename(root)
        except Exception:
            pass
    if not v:
        try:
            v = os.path.basename(os.getcwd())
        except Exception:
            v = ""
    _PROJECT = (v or "").strip().lower()[:64]
    return _PROJECT


_CONV = None


def _conv():
    """Conversation/chat id this charge belongs to — links a call back to the chat that spawned it (so per-call
    + pre/post conversation context is recoverable). Order: $SPENDGUARD_CONV / $SPENDGUARD_CHAT /
    $CLAUDE_SESSION_ID, else a stable per-process id (calls in one run share it). Cached per process."""
    global _CONV
    if _CONV is not None:
        return _CONV
    import os
    v = (os.environ.get("SPENDGUARD_CONV") or os.environ.get("SPENDGUARD_CHAT")
         or os.environ.get("CLAUDE_SESSION_ID") or "")
    if not v:
        import uuid
        v = "proc-" + uuid.uuid4().hex[:12]
    _CONV = v.strip()[:128]
    return _CONV
