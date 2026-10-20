"""Disk containment for the complete reconciliation estimate input."""
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path
import sqlite3
import tempfile
import traceback

from . import config
from .ledger import _DecSum, SpendLedger
from .ledger_read import LedgerReadError, LEDGER_READ_DECIMAL_ARITY

RECONCILE_STAGE_PREFIX = "spendguard-reconcile-stage-"
RECONCILE_STAGE_DATABASE = "estimates.db"
RECONCILE_ESTIMATE_STREAM_SQL = """SELECT id,project_primary,provider,model,day,batch_usd,cost_basis
FROM spend_events WHERE day>=? AND reconciled=0
AND COALESCE(status,'') NOT IN ('void','reversed') AND COALESCE(conv_id,'')!=?"""
RECONCILE_STAGE_LOGGER = logging.getLogger(__name__)


@contextmanager
def stage_reconcile_estimate_cells(since, correction_marker, normalize_model, path=None):
    """Read every estimate, refusing malformed providers before any records change."""
    ledger_path = Path(path if path is not None else config.db_path()).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix=RECONCILE_STAGE_PREFIX) as staging_directory:
        with closing(sqlite3.connect(str(Path(staging_directory) / RECONCILE_STAGE_DATABASE))) as staged:
            staged.create_aggregate("dec_sum", LEDGER_READ_DECIMAL_ARITY, _DecSum)
            staged.execute("PRAGMA temp_store=FILE")
            staged.execute("CREATE TABLE cells(project,provider,model,day,joined,amount)")
            staged.execute("CREATE TABLE corrections(project,provider,model,day,amount)")
            gaps = {}
            source = None
            try:
                source = config.open_ledger_connection(ledger_path.as_uri() + "?mode=ro", uri=True,
                    timeout=config.LEDGER_CONNECT_TIMEOUT_S, tune=False)
                cursor = source.execute(RECONCILE_ESTIMATE_STREAM_SQL, (since, correction_marker))
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
            except Exception as exc:
                RECONCILE_STAGE_LOGGER.exception("Reconcile estimate stream refused before correction clear")
                raise LedgerReadError(f"reconcile-estimate-stream: {exc!r}\n{traceback.format_exc()}") from exc
            finally:
                if source is not None:
                    source.close()
            for failed_provider in gaps:
                staged.execute("DELETE FROM cells WHERE provider IS ?", (failed_provider,))
            yield staged, gaps


def iter_reconcile_gate_project_days(since, path=None):
    """Stream the exact countable project/day batch projection for gap arithmetic."""
    ledger_path = Path(path if path is not None else config.db_path()).expanduser().resolve()
    source = None
    try:
        source = config.open_ledger_connection(ledger_path.as_uri() + "?mode=ro", uri=True,
            timeout=config.LEDGER_CONNECT_TIMEOUT_S, tune=False)
        source.create_aggregate("dec_sum", LEDGER_READ_DECIMAL_ARITY, _DecSum)
        for project, day, amount in source.execute(
                "SELECT project_primary,day,dec_sum(batch_usd) FROM spend_events WHERE day>=? AND "
                + SpendLedger._NOT_VOID + " AND " + SpendLedger._COUNTABLE + " GROUP BY project_primary,day",
                (since,)):
            value = float(amount)
            if value:
                yield (project or "unattributed", day), value
    except Exception as exc:
        RECONCILE_STAGE_LOGGER.exception("Reconcile project/day gap read refused")
        raise LedgerReadError(f"reconcile-project-day-stream: {exc!r}\n{traceback.format_exc()}") from exc
    finally:
        if source is not None:
            source.close()
