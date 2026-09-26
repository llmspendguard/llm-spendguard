"""Reconcile the LOCAL `calls` telemetry INTO the `spend_events` money ledger for RECORDING_GAP cells (Warden #1
part 3b) — real billed successes (cost>0, non-suspect) in `calls` that never landed in spend_events because they ran in
a non-gated / http-capture-off context (the explicit calls.record_call fired; the spend_events write did not).

SAFETY (this writes the money-of-record, so every property is deliberate):
  • DELTA only — per (intent, provider, model, day) it books calls_billed − what spent_dec ALREADY COUNTS for the cell
    (SpendLedger._COUNTABLE, the headline's own filter), never the gross, so a cell already recorded books nothing.
  • basis=billed — these are the SAME KIND as their 219K gate-recorded siblings: real metered calls with the token
    usage the provider returned, priced by the gate. They are not a projection and not plan-covered, so they belong in
    the spent_dec headline and the fill genuinely lifts it. ('reconstructed' is NOT used here — in this ledger it means
    plan-covered Claude Code usage, deliberately excluded from the real-$ headline; using it would both collide and be
    invisible to spent_dec.) The provider-truth reconcile (`spendguard reconcile`, a dev cross-check with an admin key)
    remains the separate authoritative pass and de-dupes against these by source tag.
  • usage-backed only — a cell qualifies only if its calls carry real token counts (in+out > 0); a $ with no token
    record is not provider-usage-derived and is left for the provider cross-check, never asserted as billed.
  • historical day — attributed to the day the calls happened (occurred_at), so per-day totals stay correct.
  • idempotent — a STABLE dedup_key per (intent, provider, model, day) + basis=billed (which _COUNTABLE counts) means
    re-running sees the prior fill and books nothing more.
  • reversible — source='reconcile-calls', so every row is DELETE-able WHERE source='reconcile-calls'.
  • suspect rows (part 3a — impossible per-call out_tok) are EXCLUDED from calls_billed.
No admin key, no provider call — local ledger only.
"""
from . import budget

_SOURCE = "reconcile-calls"


def _ledger_path():
    import os
    return os.path.join(os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard"), "spend.db")


# The "already-represented spend" baseline for the DELTA is EXACTLY what spent_dec counts — the ledger's own SSOT
# countable filter (SpendLedger._COUNTABLE) over its LLM $ columns (LLM_USD_COLS), imported so this can never drift
# from the headline. That single choice gets three properties at once:
#   • NO DOUBLE-COUNT — an `estimate` row already counts toward spent_dec, so a cell an estimate stands in for is NOT
#     a gap (comparing against non-estimate only — the earlier bug — re-booked ~$144 of estimated spend).
#   • IDEMPOTENCY — our fills are basis='billed' (below), which _COUNTABLE counts, so a re-run sees them and books
#     nothing more.
#   • CORRECT vs plan-covered — _COUNTABLE EXCLUDES basis='reconstructed', which here means plan-covered Claude Code
#     usage (a different kind of spend from these metered API calls). Excluding it means a plan-covered row on the
#     same (intent,model,day) never masks a real metered gap — and, since the baseline == what the headline counts,
#     the fill genuinely lifts spent_dec (the honestreview finding this resolves).
from .ledger import LLM_USD_COLS, SpendLedger   # SSOT: the countable filter + LLM $ columns, never re-implemented
_REPRESENTED_SUM = (f"COALESCE(SUM(CASE WHEN {SpendLedger._COUNTABLE} THEN "
                    + "+".join(f"COALESCE({c},0)" for c in LLM_USD_COLS)
                    + " ELSE 0 END),0) represented")


def recording_gaps(intent_like=None, min_usd=0.05):
    """The RECORDING_GAP cells: [{intent, provider, model, day, calls_billed, ledger_represented, delta}] where a
    cell's trustworthy local metered spend (cost>0, real token usage, suspect excluded) exceeds what spent_dec ALREADY
    COUNTS for it (SpendLedger._COUNTABLE, incl estimate + our own prior billed fills) by >= min_usd. Because the
    baseline IS the headline's own filter, the delta is precisely what spent_dec under-reports, the fill lifts it, and
    a re-run is idempotent. Read-only. Only usage-backed calls (in+out tokens > 0) qualify — a $ with no token record
    is not provider-usage-derived and must not be booked as basis='billed'."""
    import sqlite3
    con = sqlite3.connect(f"file:{_ledger_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # A missing spend_events table is not an error: it is the STRONGEST gap — a money ledger that was never written
    # (a fresh install, an isolated/CI home, or a fully non-gated context) means every billed `calls` row is
    # unrecorded spend. Detect it once and treat represented=$0 for every cell, rather than letting the per-cell
    # query raise. (When the table exists we read it normally below.)
    _has_se = bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone())
    where = ("WHERE intent IS NOT NULL AND cost>0 AND suspect IS NULL "
             "AND (COALESCE(in_tok,0)+COALESCE(out_tok,0))>0")   # usage-backed only → honest basis='billed'
    args = []
    if intent_like:
        where += " AND intent LIKE ?"
        args.append(f"%{intent_like}%")
    out = []
    for r in con.execute(f"""SELECT intent, provider, model, date(ts) day, SUM(cost) calls_billed
                             FROM calls {where} GROUP BY intent, provider, model, date(ts)""", args):
        represented = 0.0
        if _has_se:
            se = con.execute(f"""SELECT {_REPRESENTED_SUM}
                                 FROM spend_events WHERE intent=? AND model=? AND day=?""",
                             (r["intent"], r["model"], r["day"])).fetchone()
            represented = float(se["represented"] or 0.0)
        delta = float(r["calls_billed"] or 0.0) - represented
        if delta >= min_usd:
            out.append({"intent": r["intent"], "provider": r["provider"] or "?", "model": r["model"],
                        "day": r["day"], "calls_billed": float(r["calls_billed"] or 0.0),
                        "ledger_represented": represented, "delta": delta})
    con.close()
    return out


def reconcile(intent_like=None, min_usd=0.05, apply=False):
    """Dry-run (apply=False, default) or APPLY the calls→ledger reconciliation. Returns {cells, total_usd, applied}.
    On apply, each cell's DELTA is booked as one basis=billed spend_events row on its historical day, with a stable
    dedup_key (idempotent) and source='reconcile-calls' (reversible — DELETE WHERE source='reconcile-calls'). basis
    IS 'billed', not 'reconstructed': these are the SAME KIND as their gate-recorded siblings (real metered calls,
    provider-returned token counts, gate-priced), so they belong in the spent_dec headline; 'reconstructed' is taken
    for plan-covered Claude Code usage, which is deliberately excluded from the headline."""
    cells = recording_gaps(intent_like, min_usd)
    total = sum(c["delta"] for c in cells)
    if apply:
        for c in cells:
            oa = f"{c['day']}T12:00:00+00:00"                       # noon UTC on the historical day (deterministic)
            budget._record_spend_event(
                c["provider"], c["model"], "realtime", c["delta"],
                basis=budget.BASIS_BILLED, intent=c["intent"], occurred_at=oa, source=_SOURCE,
                dedup_key=f"{_SOURCE}:{c['intent']}:{c['provider']}:{c['model']}:{c['day']}")
    return {"cells": cells, "total_usd": total, "applied": bool(apply)}
