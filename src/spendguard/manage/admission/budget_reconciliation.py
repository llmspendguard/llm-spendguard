"""Own the provider-truth RECONCILIATION MIRROR: ``record_reconciled`` inserts a backfill row carrying the ``(provider-batch)`` sentinel so the org dashboard and leak check see provider-billed spend the gate never recorded, ``clear_reconciled`` idempotently deletes prior reconciled rows in the window after snapshotting before the destructive delete, and ``by_provider_day`` / ``reconciled_by_project`` / ``gate_by_project_day`` provide the like-for-like provider-truth comparison read by the reconcile loop. It reaches budget's engine core (``_ledger`` / ``_record_spend_event`` / ``snapshot_once``) and the read-connection aggregate decorator through the ``admission_accessors`` resolver seam, and never imports budget."""

from . import admission_accessors as _admission_accessors
from .admission_accessors import _RECONCILED, BASIS_BILLED
from ...ledger_writer import serialize_ledger_function


@_admission_accessors.resolve__consumer_aggregate_snapshot
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded COUNTABLE spend — the attributed side of reconcile. Read from
    spend_events via the single _COUNTABLE filter (reconciliation mirrors, meta, quarantine, reconstructed all
    excluded — like-for-like vs provider truth). `kind` restricts to that category's money column."""
    led = _admission_accessors.resolve__ledger()
    cols = [led.category_col(kind)] if kind else None
    res = led.sum_by(["provider", "day"], cols=cols, filt=led._COUNTABLE, since=since)
    return {(p or "?", d): float(v["usd"]) for (p, d), v in res.items() if float(v["usd"]) != 0}


@_admission_accessors.resolve__consumer_aggregate_snapshot
def reconciled_by_project(since=None):
    """{project: $} of RECONCILED batch-marker rows (recon_marker='(provider-batch)' — the provider-truth gap
    reconcile_into_ledger attributed by evidence). The 'attributed' side of the reconcile loop, complement to
    gate_by_project_day."""
    res = _admission_accessors.resolve__ledger().sum_by(
        "project_primary", since=since, where={"recon_marker": _RECONCILED})
    return {(p or "unattributed"): float(v["usd"]) for p, v in res.items()}


@_admission_accessors.resolve__consumer_aggregate_snapshot
def gate_by_project_day(kind=None, since=None):
    """{(project, day): $} of GATE-recorded (attributed) COUNTABLE spend — read from spend_events via _COUNTABLE
    (reconciliation mirrors/meta/quarantine/reconstructed excluded). Used to compute the per-project gap so the
    provider-truth gap is attributed by evidence, not dumped in one 'unattributed' bucket."""
    led = _admission_accessors.resolve__ledger()
    cols = [led.category_col(kind)] if kind else None
    res = led.sum_by(["project_primary", "day"], cols=cols, filt=led._COUNTABLE, since=since)
    return {(p or "unattributed", d): float(v["usd"]) for (p, d), v in res.items() if float(v["usd"]) != 0}


def record_reconciled(day, provider, cost, project="unattributed", kind="batch", model=None):
    """Insert a reconciliation row for provider-billed (batch) OR gate-logged (realtime) spend — the gap — attributed
    to `project` by evidence ('unattributed' only when there's none). Marked by a marker model so it's excluded from
    gate/cap and rebuilt idempotently. Default marker '(provider-batch)' / kind 'batch'; the realtime backfill passes
    its own marker + kind='realtime'."""
    marker = model or _RECONCILED
    # basis=BILLED: a reconciliation row IS the provider's own number, not a projection of ours. The marker
    # model → reconciled=1 + recon_marker, so it is excluded from the gate/cap totals.
    # invoice_id = a PERIOD-level provider-invoice reference (provider:YYYY-MM). The batch API exposes no raw invoice
    # number; this groups reconciled rows to the provider's monthly invoice (FOCUS InvoiceId), and is stamped ONLY on
    # rows we reconciled to the bill — an unreconciled estimate row keeps invoice_id NULL, so it never claims a bill.
    _admission_accessors.resolve__record_spend_event(
        provider, marker, kind, float(cost), basis=BASIS_BILLED, project=project or "unattributed",
        occurred_at=day + "T00:00:00+00:00", source="reconcile", dedup_suffix=":recon",
        invoice_id="%s:%s" % (provider, (day or "")[:7]))


@serialize_ledger_function
def clear_reconciled(since=None, model=None, providers=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them). Keyed by the marker model
    (default the batch marker; the realtime backfill passes its own)."""
    _admission_accessors.resolve_snapshot_once(
        "clear-reconciled")                 # DELETEs money rows — never without a recovery path
    marker = model or _RECONCILED
    # Delete the reconciliation-mirror rows from spend_events (the money-of-record). NOT charges: its writer
    # record_reconciled writes only spend_events (the INSERT INTO charges was removed 6 lines above in this
    # same change), reconciled_by_project reads spend_events (repointed 5b, commit 6969746), and `grep
    # 'FROM charges'` across src is EMPTY. So charges holds no reconciliation rows to leave stale.
    if providers is None:
        _admission_accessors.resolve__ledger().delete(
            where={"recon_marker": marker}, since=since, actor="clear_reconciled", reason="rebuild")
    else:
        for provider in providers:
            _admission_accessors.resolve__ledger().delete(
                where={"recon_marker": marker, "provider": provider}, since=since,
                actor="clear_reconciled", reason="rebuild")
