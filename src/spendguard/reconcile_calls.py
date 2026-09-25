"""Reconcile the LOCAL `calls` telemetry INTO the `spend_events` money ledger for RECORDING_GAP cells (Warden #1
part 3b) — real billed successes (cost>0, non-suspect) in `calls` that never landed in spend_events because they ran in
a non-gated / http-capture-off context (the explicit calls.record_call fired; the spend_events write did not).

SAFETY (this writes the money-of-record, so every property is deliberate):
  • DELTA only — per (intent, provider, model, day) it books calls_billed − spend_events_billed, never the gross, so a
    cell already recorded books nothing.
  • basis=reconstructed — clearly NOT provider-verified. The provider-truth reconcile (`spendguard reconcile`, a dev
    cross-check with an admin key) is the separate, authoritative pass; this is the local best-estimate that fills the
    hole so the ledger stops under-reporting.
  • historical day — attributed to the day the calls happened (occurred_at), so per-day totals stay correct.
  • idempotent — a STABLE dedup_key per (intent, model, day) means re-running never double-counts.
  • reversible — source='reconcile-calls', so every row is DELETE-able WHERE source='reconcile-calls'.
  • suspect rows (part 3a — impossible per-call out_tok) are EXCLUDED from calls_billed.
No admin key, no provider call — local ledger only.
"""
from . import budget

_SOURCE = "reconcile-calls"


def _ledger_path():
    import os
    return os.path.join(os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard"), "spend.db")


def recording_gaps(intent_like=None, min_usd=0.05):
    """The RECORDING_GAP cells: [{intent, provider, model, day, calls_billed, ledger_billed, delta}] where a cell's
    trustworthy local billed spend (cost>0, suspect excluded) exceeds what the money ledger recorded by >= min_usd.
    Read-only. `ledger_billed` counts every non-estimate basis (billed / reconstructed / …), so a re-run sees prior
    reconciliations and books nothing more (idempotency by construction)."""
    import sqlite3
    con = sqlite3.connect(f"file:{_ledger_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # A missing spend_events table is not an error: it is the STRONGEST gap — a money ledger that was never written
    # (a fresh install, an isolated/CI home, or a fully non-gated context) means every billed `calls` row is
    # unrecorded spend. Detect it once and treat recorded=$0 for every cell, rather than letting the per-cell query
    # raise. (When the table exists we read it normally below.)
    _has_se = bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone())
    where = "WHERE intent IS NOT NULL AND cost>0 AND suspect IS NULL"
    args = []
    if intent_like:
        where += " AND intent LIKE ?"
        args.append(f"%{intent_like}%")
    out = []
    for r in con.execute(f"""SELECT intent, provider, model, date(ts) day, SUM(cost) calls_billed
                             FROM calls {where} GROUP BY intent, provider, model, date(ts)""", args):
        se_billed = 0.0
        if _has_se:
            se = con.execute("""SELECT COALESCE(SUM(CASE WHEN cost_basis!='estimate' OR cost_basis IS NULL
                                THEN realtime_usd ELSE 0 END),0) billed
                                FROM spend_events WHERE intent=? AND model=? AND day=?""",
                             (r["intent"], r["model"], r["day"])).fetchone()
            se_billed = float(se["billed"] or 0.0)
        delta = float(r["calls_billed"] or 0.0) - se_billed
        if delta >= min_usd:
            out.append({"intent": r["intent"], "provider": r["provider"] or "?", "model": r["model"],
                        "day": r["day"], "calls_billed": float(r["calls_billed"] or 0.0),
                        "ledger_billed": se_billed, "delta": delta})
    con.close()
    return out


def reconcile(intent_like=None, min_usd=0.05, apply=False):
    """Dry-run (apply=False, default) or APPLY the calls→ledger reconciliation. Returns {cells, total_usd, applied}.
    On apply, each cell's DELTA is booked as one basis=reconstructed spend_events row on its historical day, with a
    stable dedup_key (idempotent) and source='reconcile-calls' (reversible)."""
    cells = recording_gaps(intent_like, min_usd)
    total = sum(c["delta"] for c in cells)
    if apply:
        for c in cells:
            oa = f"{c['day']}T12:00:00+00:00"                       # noon UTC on the historical day (deterministic)
            budget._record_spend_event(
                c["provider"], c["model"], "realtime", c["delta"],
                basis=budget.BASIS_RECONSTRUCTED, intent=c["intent"], occurred_at=oa, source=_SOURCE,
                dedup_key=f"{_SOURCE}:{c['intent']}:{c['model']}:{c['day']}")
    return {"cells": cells, "total_usd": total, "applied": bool(apply)}
