"""Best-effort WAL reclamation with explicit contention and failure outcomes."""
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from spendguard import config

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


if hasattr(config.os, "register_at_fork"):
    config.os.register_at_fork(after_in_child=_reset_ledger_wal_cadence_in_child)


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
    from .ledger_read import effective_ledger_admission_deadline, LedgerReadError
    duration = config.LEDGER_READ_TIME_BUDGET_S if deadline_s is None else deadline_s
    connection = None
    try:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("WAL checkpoint deadline must be positive and finite")
        deadline_at = effective_ledger_admission_deadline(time.monotonic() + duration)
        busy_timeout_ms = config.ledger_wal_checkpoint_busy_timeout_ms()
        checkpoint_deadline_at = min(deadline_at, time.monotonic()
                                     + busy_timeout_ms / config.LEDGER_TIMEOUT_MS_PER_S)
        connection = config.open_ledger_connection(
            path, timeout=busy_timeout_ms / config.LEDGER_TIMEOUT_MS_PER_S,
            tune=False, deadline_at=checkpoint_deadline_at, isolation_level=None)
        # SQLite's busy handler does not run the VM progress handler. Bound it
        # separately to the remaining admission/query deadline as well as cadence.
        remaining_ms = max(0, int((checkpoint_deadline_at - time.monotonic()) * config.LEDGER_TIMEOUT_MS_PER_S))
        connection.execute(f"PRAGMA busy_timeout={min(busy_timeout_ms, remaining_ms)}")
        connection.set_progress_handler(lambda: time.monotonic() >= checkpoint_deadline_at,
                                        config.LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS)
        busy, log_frames, checkpointed_frames = connection.execute(WAL_CHECKPOINT_SQL).fetchone()
        if time.monotonic() >= deadline_at:
            raise TimeoutError("WAL checkpoint deadline exceeded")
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_BUSY if busy else WAL_CHECKPOINT_SUCCESS,
                                          busy, log_frames, checkpointed_frames)
    except LedgerReadError:
        raise
    except (Exception, config.LedgerConnectionLimitError) as error:
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
    from .ledger_read import LedgerReadError, effective_ledger_admission_deadline
    effective_ledger_admission_deadline()
    if cadence is None:
        cadence = _DEFAULT_WAL_MAINTENANCE_CADENCE
    if not cadence.lock.acquire(blocking=False):
        return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
    try:
        ledger = str(Path(path or config.db_path()).resolve())
        interval = config.ledger_wal_checkpoint_interval_s()
        now = time.monotonic()
        last_attempt = cadence.attempts.get(ledger)
        if last_attempt is not None and now - last_attempt < interval:
            return LedgerWalCheckpointReport(WAL_CHECKPOINT_SKIPPED)
        cadence.attempts[ledger] = now
        from .ledger_daily_rollup import schedule_rollup_fingerprint_audit
        schedule_rollup_fingerprint_audit(ledger)
        return truncate_ledger_wal_bounded(ledger)
    except LedgerReadError:
        raise
    except (Exception, config.LedgerConnectionLimitError) as error:
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_FAILED, error=f"{type(error).__name__}: {error}")
        _WAL_MAINTENANCE_LOG.warning("Ledger WAL maintenance: %s", report)
        return report
    finally:
        cadence.lock.release()
