"""Own the estimate→actual TRUE-DOWN lifecycle: ``gate_batch_cells`` provides the gate-live batch estimate base while excluding reconcile markers and prior corrections for idempotence; ``record_true_down`` inserts ONE negative batch correction row carrying the real model and true-down conv sentinel without mutating original estimates; and ``clear_true_down`` / ``clear_readable_true_down`` delete prior true-down rows in the window so each reconcile rebuilds the correction idempotently, unconditionally by design. It reaches budget's engine core (``_ledger`` / ``_record_spend_event`` / ``snapshot_once``) and the read-connection aggregate decorator through the ``admission_accessors`` resolver seam, and never imports budget."""

from . import admission_accessors as _admission_accessors
from .admission_accessors import _TRUE_DOWN_CONV


# RAW-CHARGES-OK: excludes true-down as well, which the view deliberately keeps
@_admission_accessors.resolve__consumer_aggregate_snapshot
def gate_batch_cells(since=None):
    """{(project, provider, model, day): $} of GATE-LIVE batch rows — the estimate base the true-down corrects.
    Excludes every reconcile marker model AND prior true-down rows (idempotence: corrections never feed the next
    correction). Full-dimension sibling of gate_by_project_day."""
    led = _admission_accessors.resolve__ledger()
    # sum the BATCH money column only; exclude reconciliation markers (reconciled=0) and prior true-down rows
    # (the true-down conv sentinel) so a correction never feeds the next correction. Quarantine (void) is auto-out.
    filt = "COALESCE(conv_id,'') != '%s'" % _TRUE_DOWN_CONV
    res = led.sum_by(["project_primary", "provider", "model", "day"], cols=["batch_usd"],
                     where={"reconciled": 0}, filt=filt, since=since)
    return {(pr or "unattributed", p or "?", m or "?", d): float(v["usd"])
            for (pr, p, m, d), v in res.items() if float(v["usd"]) != 0}


def record_true_down(day, provider, model, delta, project, *, maintenance_ledger=None, posting_metadata=None):
    """Insert ONE negative batch correction row: estimate − billed for this cell's share. Carries the REAL model
    (so by_dims nets it against the estimate rows before the SaaS push) and the true-down conv_id sentinel (so it
    is identifiable/clearable without a marker model). The original estimate rows are NEVER mutated — the ledger
    keeps both the estimate and the correction (forensic: what we thought + what it actually billed)."""
    # A NEGATIVE batch row carrying the REAL model + the true-down conv sentinel (nets the estimate down).
    # gate_batch_cells (the estimate base) and clear_true_down were BOTH repointed to spend_events in 5b (commit
    # 6969746), and a `grep 'FROM charges'` sweep across src is EMPTY — no reader reads charges, so writing only
    # spend_events here is complete, not partial.
    _admission_accessors.resolve__record_spend_event(
        provider, model, "batch", -abs(float(delta)), conv_id=_TRUE_DOWN_CONV,
        project=project or "unattributed", occurred_at=day + "T00:00:00+00:00",
        source="true-down", dedup_suffix=":td",
        invoice_id="%s:%s" % (provider, (day or "")[:7]),
        maintenance_ledger=maintenance_ledger,
        posting_metadata=posting_metadata)   # reconciled to this provider-period invoice


def clear_true_down(since=None):
    """Clear every provider unconditionally: an estimate is more honest than stale billed truth.

    A failed billed fetch must fall back to estimates, never retain an old correction.
    Ledger-read failures use the separate readable-provider rebuild operation instead.
    """
    return clear_readable_true_down(since)


def clear_readable_true_down(since=None, providers=None, *, maintenance_ledger=None):
    """Remove prior true-down rows in the window so the correction is rebuilt idempotently each reconcile
    (billed actuals only grow as in-flight batches land, so each rebuild converges on the final billed $).

    CLEARS EVERY PROVIDER ON PURPOSE, including one whose billed fetch just failed. A review finding said
    this was a bug — clearing corrections for a provider we could not read — and test_true_down.py, which
    predates it, says otherwise and explains why: a failed fetch means that provider is NEVER trued down,
    so its rows fall back to the gate ESTIMATE, which is labelled `basis=estimate` and reads as one.
    Keeping the previous correction would leave a figure LABELLED as billed truth while being derived from
    billed data this run could not confirm — a stale number wearing a stronger label. The estimate is the
    more honest of the two, so the clear stays unconditional and ledger_sync warns about the gap instead.
    Second finding today that both validators confirmed and the code was right about.

    An explicit providers collection scopes the rebuild to readable provider units; None preserves the
    unconditional default above. An empty collection changes nothing, including taking no snapshot."""
    from ...ledger_writer import current_ledger_writer_connection, run_ledger_write
    if maintenance_ledger is None and current_ledger_writer_connection() is None:
        return run_ledger_write(lambda: clear_readable_true_down(since, providers))
    true_down_provider_scope = None if providers is None else tuple(dict.fromkeys(providers))
    if true_down_provider_scope == ():
        return
    _admission_accessors.resolve_snapshot_once("clear-true-down")    # DELETEs money rows — never without a recovery path
    # Delete the true-down correction rows from spend_events (the money-of-record). NOT charges: the ONLY writer
    # of true-down rows, record_true_down, now writes only spend_events (its INSERT INTO charges was removed
    # above); and the only consumers of these rows — gate_batch_cells (the estimate base) and by_dims (the SaaS
    # push) — read spend_events (repointed 5b). So charges holds no true-down rows for this to leave stale.
    from ...ledger import LockedError
    from spendguard.store.spend_search.ledger_read_accessors import resolve_default_ledger_read_row_cap
    import logging as true_down_logging
    import traceback as true_down_traceback
    true_down_ledger = maintenance_ledger if maintenance_ledger is not None else _admission_accessors.resolve__ledger()
    true_down_scopes = ([{"conv_id": _TRUE_DOWN_CONV}] if true_down_provider_scope is None else
                        [{"conv_id": _TRUE_DOWN_CONV, "provider": true_down_provider}
                         for true_down_provider in true_down_provider_scope])
    try:
        # Preserve delete's full lock preflight before the first destructive batch, without retaining all matches.
        for true_down_scope in true_down_scopes:
            true_down_filter, true_down_args = true_down_ledger._where(since, None, true_down_scope)
            true_down_cursor = true_down_ledger._conn.execute(
                f"SELECT id, period FROM spend_events WHERE 1=1{true_down_filter} "
                "AND (COALESCE(locked,0) != 0 OR "
                "(COALESCE(period,'') != '' AND period <= (SELECT MAX(period) FROM ledger_locks)))",
                true_down_args)
            try:
                true_down_locked_row = true_down_cursor.fetchone()
                if true_down_locked_row:
                    true_down_id, true_down_period = true_down_locked_row
                    raise LockedError(f"refusing to delete locked event {true_down_id} "
                                      f"(period {true_down_period}) — use reverse/adjust")
            finally:
                true_down_cursor.close()
        for true_down_scope in true_down_scopes:
            true_down_filter, true_down_args = true_down_ledger._where(since, None, true_down_scope)
            while True:
                true_down_rowids = true_down_ledger._conn.execute(
                    f"SELECT rowid FROM spend_events WHERE 1=1{true_down_filter} LIMIT ?",
                    (*true_down_args, resolve_default_ledger_read_row_cap())).fetchall()
                if not true_down_rowids:
                    break
                # rowid is a SQLite integer, not source content; parse before forming the internal SQL predicate.
                true_down_rowid_sql = ",".join(str(int(true_down_rowid[0])) for true_down_rowid in true_down_rowids)
                del true_down_rowids  # release this batch before delete() allocates its bounded audit batch.
                true_down_ledger.delete(where=true_down_scope, filt=f"rowid IN ({true_down_rowid_sql})", since=since,
                                        actor="clear_true_down", reason="rebuild")
    except Exception as true_down_clear_error:
        true_down_logging.getLogger(__name__).exception("refusing clear_true_down after ledger clear failure")
        if maintenance_ledger is not None or isinstance(true_down_clear_error, LockedError):
            raise
        raise RuntimeError(
            f"refusing clear_true_down after ledger clear failure: {true_down_clear_error!r}\n"
            f"{true_down_traceback.format_exc()}") from true_down_clear_error
