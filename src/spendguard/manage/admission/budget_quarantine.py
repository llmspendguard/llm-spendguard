"""Own the QUARANTINE lifecycle of a single spend_events row: ``quarantine_charge`` voids an impossible-estimate row so it is dropped from every total while remaining fully auditable through the ledger's hash-chained update, and ``unquarantine_charge`` reverses a wrongful void from ``status='void'`` to ``status='posted'`` while clearing the ``(impossible-estimate)`` label; both serialize through the ledger writer, refuse a locked period, reach budget's engine core (``_ledger``/``_lock``) through the ``admission_accessors`` resolver seam, and never import budget."""

from . import admission_accessors as _admission_accessors
from ...ledger_writer import serialize_ledger_function
from .admission_accessors import QUARANTINE_CONV


@serialize_ledger_function
def quarantine_charge(ts=None, reason="", row=None):
    """Void ONE spend_events row as an impossible estimate — status='void', so it drops out of every total
    while staying fully auditable (the amount is untouched; the change is logged through the hash chain by
    update()).

    Target by `row` (the spend_events id — exact) or by `ts` (ts_utc). Up to six rows can share one second, so
    a ts matching more than one RAISES rather than voiding all of them: silently voiding five innocent rows to
    quarantine one bad one would be a worse version of the bug this repairs. Returns the number voided."""
    from ...ledger import to_dec, USD_COLS
    led = _admission_accessors.resolve__ledger()
    with _admission_accessors.resolve__lock():
        if row is not None:
            hit = led.get(row)
            ids = [row] if hit and hit.get("status") != "void" else []
        else:
            hits = [r for r in led.query(where={"ts_utc": ts}) if r.get("status") != "void"]
            if len(hits) > 1:
                raise ValueError(
                    "%d spend_events rows share the timestamp %s (%s) — refusing to void all of them. Re-run "
                    "with --row <id> for the one you mean; `spendguard quarantine --list` shows the ids."
                    % (len(hits), ts, ", ".join("id %s: %s $%s" % (
                        r["id"], r["model"], sum((to_dec(r.get(c)) for c in USD_COLS), to_dec(0))) for r in hits)))
            ids = [hits[0]["id"]] if hits else []
        n = 0
        for eid in ids:
            # update() sets status AND writes the audit row through the hash chain, under the ledger's own
            # connection — the mutation and its tamper record are one operation. It refuses a locked period.
            led.update(eid, {"status": "void"}, actor="quarantine_charge",
                       reason=reason or "impossible estimate", pass_="quarantine")
            n += 1
    return n


@serialize_ledger_function
def unquarantine_charge(row=None, ts=None, reason=""):
    """Reverse quarantine_charge for ONE spend_events row: status 'void'→'posted' so it counts again, and clear
    the '(impossible-estimate)' conv_id label it was tagged with. The change is logged through the hash chain by
    update() (so the void→recovered history is auditable), and a locked period is refused.

    Use ONLY for a row that was wrongly voided. The sound test is `basis == billed` with a real cost: a cost that
    came from the provider's OWN usage means the provider ACCEPTED the request, so it cannot have been an
    impossible estimate — the batched-embedding bug quarantined exactly these (input > window computed as sum/1,
    when the window bounds each item). A cost_basis='estimate' void is a pre-submission projection the provider
    never accepted; those stay quarantined. Target by `row` (exact id) or `ts` (a ts matching >1 RAISES, as in
    quarantine_charge). Returns the number recovered."""
    led = _admission_accessors.resolve__ledger()
    with _admission_accessors.resolve__lock():
        if row is not None:
            hit = led.get(row)
            ids = [row] if hit and hit.get("status") == "void" else []
        else:
            hits = [r for r in led.query(where={"ts_utc": ts}) if r.get("status") == "void"]
            if len(hits) > 1:
                raise ValueError(
                    "%d spend_events rows share the timestamp %s — refusing to recover all of them. Re-run with "
                    "--row <id>; `spendguard quarantine --list` shows the ids." % (len(hits), ts))
            ids = [hits[0]["id"]] if hits else []
        n = 0
        for eid in ids:
            cur = led.get(eid) or {}
            changes = {"status": "posted"}
            if cur.get("conv_id") == QUARANTINE_CONV:      # drop the now-false label; keep it if it was something else
                changes["conv_id"] = ""
            led.update(eid, changes, actor="unquarantine_charge",
                       reason=reason or "recovered: provider-billed, wrongly quarantined", pass_="unquarantine")
            n += 1
    return n
