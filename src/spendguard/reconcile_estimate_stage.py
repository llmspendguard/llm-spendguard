"""Disk containment for the complete reconciliation estimate input."""
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import traceback

from . import config
from .ledger import _DecSum, SpendLedger
from .ledger_read import LedgerReadError, LEDGER_READ_DECIMAL_ARITY, _ledger_read_connection

RECONCILE_STAGE_PREFIX = "spendguard-reconcile-stage-"
RECONCILE_STAGE_DATABASE = "estimates.db"
RECONCILE_ESTIMATE_BATCH_KIND = "batch"
RECONCILE_ESTIMATE_STREAM_SQL = """SELECT id,project_primary,provider,model,day,batch_usd,cost_basis
FROM spend_events WHERE day>=? AND reconciled=0
AND COALESCE(status,'') NOT IN ('void','reversed') AND COALESCE(conv_id,'')!=?
AND (batch_usd IS NOT NULL OR cost_type IS NULL OR cost_type=?)"""
RECONCILE_STAGE_LOGGER = logging.getLogger(__name__)
# Offline scans own their complete lifetime. Large ledgers can configure the
# whole-scan budget; expiration refuses the complete result before correction.
RECONCILE_BULK_READ_TIME_BUDGET_S = float(os.getenv(
    "SPENDGUARD_RECONCILE_BULK_READ_TIME_BUDGET_S", str(config.LEDGER_READ_TIME_BUDGET_S)))


@contextmanager
def stage_reconcile_estimate_cells(since, correction_marker, normalize_model, path=None,
                                   time_budget=RECONCILE_BULK_READ_TIME_BUDGET_S):
    """Read every estimate, refusing malformed providers before any records change."""
    ledger_path = Path(path if path is not None else config.db_path()).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix=RECONCILE_STAGE_PREFIX) as staging_directory:
        with closing(sqlite3.connect(str(Path(staging_directory) / RECONCILE_STAGE_DATABASE))) as staged:
            staged.create_aggregate("dec_sum", LEDGER_READ_DECIMAL_ARITY, _DecSum)
            staged.execute("PRAGMA temp_store=FILE")
            staged.execute("CREATE TABLE cells(project,provider,model,day,joined,amount)")
            staged.execute("CREATE TABLE corrections(project,provider,model,day,amount)")
            gaps = {}
            try:
                with _ledger_read_connection(ledger_path, time_budget) as source:
                    with closing(sqlite3.Connection.cursor(source, sqlite3.Cursor)) as source_cursor:
                        _populate_reconcile_estimate_stage(source_cursor, staged, gaps, since,
                                                           correction_marker, normalize_model)
            except LedgerReadError:
                raise
            except Exception as exc:
                RECONCILE_STAGE_LOGGER.exception("Reconcile estimate stream refused before correction clear")
                raise LedgerReadError(f"reconcile-estimate-stream: {exc!r}\n{traceback.format_exc()}") from exc
            for failed_provider in gaps:
                staged.execute("DELETE FROM cells WHERE provider IS ?", (failed_provider,))
            yield staged, gaps


def _populate_reconcile_estimate_stage(source_cursor, staged, gaps, since, correction_marker, normalize_model):
    """Stream an offline owner's native cursor without per-row request dispatch."""
    # A different typed money axis has an absent batch column, not an
    # unknown batch price. Keep explicit batch and legacy untyped rows
    # even when their price is NULL; those remain incomplete evidence.
    cursor = source_cursor.execute(RECONCILE_ESTIMATE_STREAM_SQL,
        (since, correction_marker, RECONCILE_ESTIMATE_BATCH_KIND))
    for row_id, project, provider, model, day, amount, basis in cursor:
        # Keep unknown prices as NULL, including writer zero placeholders.
        if amount is None or basis == "unpriced":
            staged.execute("INSERT INTO cells VALUES(?,?,?,?,?,?)",
                (project, provider, model, day, normalize_model(model), None))
            continue
        try:
            value = Decimal(str(amount))
        except InvalidOperation as exc:
            RECONCILE_STAGE_LOGGER.exception("Reconcile estimate row refused: id=%r provider=%r day=%r",
                                           row_id, provider, day)
            provider_gap = gaps.setdefault(provider, {"unreadable_rows": 0, "reason":
                f"estimate-row:{row_id}:provider={provider!r}:day={day!r}: {exc!r}\n{traceback.format_exc()}"})
            provider_gap["unreadable_rows"] += 1
            continue
        if not value.is_finite():
            raise ValueError(f"nonfinite batch amount {amount!r}")
        staged.execute("INSERT INTO cells VALUES(?,?,?,?,?,?)",
            (project, provider, model, day, normalize_model(model), str(value)))
    staged.commit()


def iter_reconcile_gate_project_days(since, path=None, providers=None, include_provider=False,
                                     time_budget=RECONCILE_BULK_READ_TIME_BUDGET_S):
    """Stream the exact countable project/day batch projection for gap arithmetic."""
    ledger_path = Path(path if path is not None else config.db_path()).expanduser().resolve()
    try:
        with _ledger_read_connection(ledger_path, time_budget) as source:
            with closing(sqlite3.Connection.cursor(source, sqlite3.Cursor)) as source_cursor:
                provider_scope = tuple(providers) if providers is not None else None
                if provider_scope == ():
                    return
                scope_sql = ""
                parameters = [since]
                if provider_scope is not None:
                    scope_sql = " AND provider IN (" + ",".join("?" for _ in provider_scope) + ")"
                    parameters.extend(provider_scope)
                grouping = "provider,project_primary,day" if include_provider else "project_primary,day"
                for provider, project, day, amount in source_cursor.execute(
                        "SELECT provider,project_primary,day,dec_sum(batch_usd) FROM spend_events WHERE day>=? AND "
                        + SpendLedger._NOT_VOID + " AND " + SpendLedger._COUNTABLE + scope_sql
                        + " GROUP BY " + grouping, parameters):
                    value = float(amount)
                    if value:
                        key = (provider, project or "unattributed", day) if include_provider else (project or "unattributed", day)
                        yield key, value
    except LedgerReadError:
        raise
    except Exception as exc:
        RECONCILE_STAGE_LOGGER.exception("Reconcile project/day gap read refused")
        raise LedgerReadError(f"reconcile-project-day-stream: {exc!r}\n{traceback.format_exc()}") from exc
