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


@dataclass(frozen=True)
class LedgerWalCheckpointReport:
    """Named outcome and the complete SQLite checkpoint counters."""
    status: str
    busy: int | None = None
    log_frames: int | None = None
    checkpointed_frames: int | None = None
    error: str | None = None


def truncate_ledger_wal_bounded(path=None, *, deadline_s=None):
    """Attempt one TRUNCATE with zero busy waiting; close on every outcome.

    Managed admission and the progress handler share the read deadline. SQLite
    filesystem I/O itself remains subject to OS scheduling, as with ledger opens.
    No row contents change; contention is a visible BUSY report, never retried inline.
    """
    duration = config.LEDGER_READ_TIME_BUDGET_S if deadline_s is None else deadline_s
    connection = None
    try:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("WAL checkpoint deadline must be positive and finite")
        deadline_at = time.monotonic() + duration
        connection = config.open_ledger_connection(
            path, timeout=config.LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS,
            tune=False, deadline_at=deadline_at, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={config.LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS}")
        connection.set_progress_handler(lambda: time.monotonic() >= deadline_at,
                                        config.LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS)
        busy, log_frames, checkpointed_frames = connection.execute(WAL_CHECKPOINT_SQL).fetchone()
        if time.monotonic() >= deadline_at:
            raise TimeoutError("WAL checkpoint deadline exceeded")
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_BUSY if busy else WAL_CHECKPOINT_SUCCESS,
                                          busy, log_frames, checkpointed_frames)
    except (Exception, config.LedgerConnectionLimitError) as error:
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_FAILED, error=f"{type(error).__name__}: {error}")
    finally:
        if connection is not None:
            connection.close()
    if report.status != WAL_CHECKPOINT_SUCCESS:
        _WAL_MAINTENANCE_LOG.warning("Ledger WAL checkpoint: %s", report)
    return report


def maintain_ledger_wal_if_due(path=None, *, cadence=_DEFAULT_WAL_MAINTENANCE_CADENCE):
    """Run at the configured cadence after pool open or a successful operation.

    The process lock is nonblocking so concurrent callers never queue behind
    maintenance. BUSY/failed attempts are logged and retried on a later operation.
    """
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
        return truncate_ledger_wal_bounded(ledger)
    except (Exception, config.LedgerConnectionLimitError) as error:
        report = LedgerWalCheckpointReport(WAL_CHECKPOINT_FAILED, error=f"{type(error).__name__}: {error}")
        _WAL_MAINTENANCE_LOG.warning("Ledger WAL maintenance: %s", report)
        return report
    finally:
        cadence.lock.release()
