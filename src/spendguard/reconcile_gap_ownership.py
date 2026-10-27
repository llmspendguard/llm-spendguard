"""Recover gap ownership from row evidence; ``spend_events.provider`` is the SSOT.

Invoice references are evidence, not a second ownership axis. Migration changes
only provider metadata and its audit trail, under one snapshotted transaction.
"""
from datetime import datetime
import copy
from types import MethodType

from . import budget, pricing
from .ledger_writer import serialize_ledger_function, current_ledger_writer_connection

RECONCILE_GAP_UNOWNED_VALUES = frozenset((None, "", "?", "unknown", "unattributed", "(reconciled)"))
RECONCILE_GAP_INVOICE_SEPARATOR = ":"
RECONCILE_GAP_PERIOD_FORMAT = "%Y-%m"
RECONCILE_GAP_OWNERSHIP_ACTOR = "reconcile-gap-ownership"
RECONCILE_GAP_UNKNOWN_REASON = "no-row-local-provider-evidence"
RECONCILE_GAP_LOCKED_REASON = "ownership-metadata-locked"


def derive_reconcile_gap_row_owner(row):
    """Parse a provider-period invoice or look up the exact existing model catalog.

    No model prefix inference is used: an absent catalog entry remains unknown.
    Conflicting invoice/catalog evidence remains unknown as well.
    """
    invoice = row["invoice_id"] or ""
    owner, separator, period = invoice.partition(RECONCILE_GAP_INVOICE_SEPARATOR)
    invoice_owner = None
    if separator and owner not in RECONCILE_GAP_UNOWNED_VALUES:
        try:
            if datetime.strptime(period, RECONCILE_GAP_PERIOD_FORMAT).strftime(RECONCILE_GAP_PERIOD_FORMAT) == period:
                invoice_owner = owner
        except ValueError:
            pass
    model = row["model"] or ""
    catalog_owner = pricing.PROVIDERS.get(model) or pricing.PROVIDERS.get(pricing.normalize(model))
    if invoice_owner and catalog_owner and invoice_owner != catalog_owner:
        return None
    return invoice_owner or catalog_owner


def migrate_reconcile_gap_provider_ownership(since=None):
    """Backfill all derivable gaps atomically; report every remaining unowned row.

    Streaming scans have no admission cap. Snapshot and audit failures propagate;
    no incomplete migration can be mistaken for a completed ownership decision.
    """
    return apply_reconcile_gap_provider_ownership(budget._ledger(), since)


@serialize_ledger_function
def apply_reconcile_gap_provider_ownership(source_ledger, since):
    """Apply the caller's snapshotted ledger facade on the owner connection."""
    result = {"backfilled": 0, "unowned": 0, "gaps": {}}
    with budget._lock:
        ledger = copy.copy(source_ledger)
        audit_override = source_ledger.__dict__.get("_audit")
        if getattr(audit_override, "__self__", None) is source_ledger:
            ledger._audit = MethodType(audit_override.__func__, ledger)
        connection = current_ledger_writer_connection(ledger.db_path)
        ledger._conn = connection
        condition = "recon_marker=?"
        parameters = [budget._RECONCILED]
        if since is not None:
            condition += " AND day>=?"
            parameters.append(since)
        budget.snapshot_once(RECONCILE_GAP_OWNERSHIP_ACTOR)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in connection.execute("SELECT * FROM spend_events WHERE " + condition, parameters):
                if row["provider"] not in RECONCILE_GAP_UNOWNED_VALUES:
                    continue
                owner = derive_reconcile_gap_row_owner(row)
                reason = RECONCILE_GAP_UNKNOWN_REASON
                if owner and ledger._is_locked(row):
                    owner = None
                    reason = RECONCILE_GAP_LOCKED_REASON
                if not owner:
                    result["unowned"] += 1
                    result["gaps"][reason] = result["gaps"].get(reason, 0) + 1
                    continue
                connection.execute("UPDATE spend_events SET provider=?,revision=revision+1 WHERE id=?",
                                   (owner, row["id"]))
                ledger._audit(row["id"], RECONCILE_GAP_OWNERSHIP_ACTOR, RECONCILE_GAP_OWNERSHIP_ACTOR,
                              "provider", row["provider"], owner, "row-local ownership evidence")
                result["backfilled"] += 1
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return result
