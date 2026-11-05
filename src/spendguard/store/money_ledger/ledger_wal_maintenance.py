"""Best-effort WAL reclamation with explicit contention and failure outcomes."""
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from spendguard.store.configuration import ledger_connection_accessors
from spendguard.store.spend_search import ledger_read_accessors

_WAL_MAINTENANCE_LOG = logging.getLogger(__name__)
WAL_CHECKPOINT_SUCCESS = "complete"
WAL_CHECKPOINT_BUSY = "busy"
WAL_CHECKPOINT_FAILED = "failed"
WAL_CHECKPOINT_SKIPPED = "not_due"
WAL_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"


@dataclass
class LedgerWalMaintenanceCadence:
    """Explicit owner of checkpoint admission and per-ledger cadence."""
    lock: object = field(default_factory=threading.Lock)
    attempts: dict = field(default_factory=dict)


_DEFAULT_WAL_MAINTENANCE_CADENCE = LedgerWalMaintenanceCadence()


def _reset_ledger_wal_cadence_in_child():
    global _DEFAULT_WAL_MAINTENANCE_CADENCE
    _DEFAULT_WAL_MAINTENANCE_CADENCE = LedgerWalMaintenanceCadence()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_ledger_wal_cadence_in_child)


@dataclass(frozen=True)
class LedgerWalCheckpointReport:
    """Named outcome and the complete SQLite checkpoint counters."""
    status: str
    busy: int | None = None
    log_frames: int | None = None
    checkpointed_frames: int | None = None
    error: str | None = None


def truncate_ledger_wal_bounded(path=None, *, deadline_s=None):
    """Attempt one TRUNCATE with a bounded lock wait; close on every outcome.

    Admission and SQLite lock waiting share one short budget, capped by the read
    deadline; the progress handler observes that same short budget. SQLite
    filesystem I/O itself remains subject to OS scheduling, as with ledger opens.
    No row contents change; contention is a visible BUSY report, never retried inline.
    """
    duration = (ledger_connection_accessors.get_wal_ledger_read_time_budget_seconds()
                if deadline_s is None else deadline_s)
    connection = None
    try:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("WAL checkpoint deadline must be positive and finite")
        deadline_at = ledger_read_accessors.resolve_ledger_admission_deadline(time.monotonic() + duration)
        busy_timeout_ms = ledger_connection_accessors.read_wal_checkpoint_busy_timeout_milliseconds()
        timeout_ms_per_s = ledger_connection_accessors.get_wal_ledger_timeout_milliseconds_per_second()
        checkpoint_deadline_at = min(deadline_at, time.monotonic()
                                     + busy_timeout_ms / timeout_ms_per_s)
        connection = ledger_connection_accessors.open_wal_ledger_connection(
            path, timeout=busy_timeout_ms / timeout_ms_per_s,
            tune=False, deadline_at=checkpoint_deadline_at, isolation_level=None)
        # SQLite's busy handler does not run the VM progress handler. Bound it
        # separately to the remaining admission/query deadline as well as cadence.
        remaining_ms = max(0, int((checkpoint_deadline_at - time.monotonic()) * timeout_ms_per_s))
        connection.execute(f"PRAGMA busy_timeout={min(busy_timeout_ms, remaining_ms)}")
        connection.set_progress_handler(lambda: time.monotonic() >= checkpoint_deadline_at,
                                        ledger_connection_accessors.get_wal_ledger_query_progress_vm_instructions())
        busy, log_frames, checkpointed_frames = connection.execute(WAL_CHECKPOINT_SQL).fetchone()
        if time.monotonic() >= deadline_at:
            raise TimeoutError("WAL checkpoint deadline exceeded")
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_BUSY if busy else WAL_CHECKPOINT_SUCCESS,
                                          busy, log_frames, checkpointed_frames)
    except ledger_read_accessors.ledger_read_error_type():
        raise
    except (Exception, ledger_connection_accessors.wal_ledger_connection_limit_error_type()) as error:
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_FAILED, error=f"{type(error).__name__}: {error}")
    finally:
        if connection is not None:
            connection.close()
    if report.status != WAL_CHECKPOINT_SUCCESS:
        _WAL_MAINTENANCE_LOG.warning("Ledger WAL checkpoint: %s", report)
    return report


def maintain_ledger_wal_if_due(path=None, *, cadence=None):
    """Run at the configured cadence after pool open or a successful operation.

    The process lock is nonblocking so concurrent callers never queue behind
    maintenance. BUSY/failed attempts are logged and retried on a later operation.
    """
    # Best-effort maintenance must NEVER abort the serving operation that happens to trigger it. A serving
    # surface can hold an expired/abandoned request scope (e.g. a long inline delegate turn ran under a
    # request-lifetime ledger_request_scope whose absolute deadline has since passed); raising that request's
    # deadline out of a checkpoint would fail an otherwise-complete pool open (config.pooled_ledger_conn and
    # ledger_op re-raise it). With no live budget there is nothing to reclaim within bound anyway, so SKIP and
    # let a later unpressured open run it — SQLite's own wal_autocheckpoint still bounds the WAL meanwhile.
    try:
        ledger_read_accessors.resolve_ledger_admission_deadline()
    except ledger_read_accessors.ledger_read_error_type():
        return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
    if cadence is None:
        cadence = _DEFAULT_WAL_MAINTENANCE_CADENCE
    if not cadence.lock.acquire(blocking=False):
        return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
    try:
        ledger = str(Path(path or ledger_connection_accessors.resolve_wal_ledger_db_path()).resolve())
        interval = ledger_connection_accessors.read_wal_checkpoint_interval_seconds()
        now = time.monotonic()
        last_attempt = cadence.attempts.get(ledger)
        if last_attempt is not None and now - last_attempt < interval:
            return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
        cadence.attempts[ledger] = now
        from .ledger_daily_rollup import schedule_rollup_fingerprint_audit
        schedule_rollup_fingerprint_audit(ledger)
        return truncate_ledger_wal_bounded(ledger)
    except ledger_read_accessors.ledger_read_error_type():
        # The ambient request expired/aborted mid-checkpoint: best-effort maintenance SKIPS rather than
        # propagating the serving caller's deadline out of a non-mandatory checkpoint (see the pre-check above).
        return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
    except (Exception, ledger_connection_accessors.wal_ledger_connection_limit_error_type()) as error:
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_FAILED, error=f"{type(error).__name__}: {error}")
        _WAL_MAINTENANCE_LOG.warning("Ledger WAL maintenance: %s", report)
        return report
    finally:
        cadence.lock.release()
