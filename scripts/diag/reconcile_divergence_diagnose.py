"""DIAGNOSE (read-only, $0, no admin key) a per-call `calls` vs money-ledger `spend_events` divergence, and PROPERLY
DETERMINE ITS CAUSE before anyone writes the ledger (Ash 2026-09-25: "if there is an error in prompt or other that
could be the cause, we should be able to properly determine this").

The Warden #1 symptom: a metered intent's actuals appear in `calls` but the money ledger under-reports. The cause is
NOT one thing, and copying `calls`->`spend_events` blindly would corrupt attribution. This classifies each (intent,
model, day) cell by CAUSE so the right fix is obvious per cell:

  • RECORDING_GAP   — real SUCCESSES (cost>0, finish=stop, output present) in `calls` with NO spend_events row at all.
                      The money ledger genuinely missed billed spend (likely a non-gated / http-capture-off context).
  • PAID_NO_OUTPUT  — cost>0 but NO output / finish in (None, length): paid for an ERROR or an empty-reasoning reply.
                      These are the "error in prompt or other" cells — surfaced so a prompt/again-billed bug is caught,
                      NOT silently reconciled as if they were good output.
  • UNRECONCILED_ESTIMATE — a spend_events cost_basis='estimate' row that never trued-down to a billed actual.
  • SUSPECT_EXCLUDED — `calls` rows flagged suspect (impossible per-call out_tok) are excluded from every number here.
  • OK              — calls success $ ~matches spend_events billed $ (no material gap).

Usage:
  ./.venv.nosync/bin/python scripts/diag/reconcile_divergence_diagnose.py [--intent <intent-substr>] [--min-usd 0.01]
No writes. Uses the local ledger only (no provider call, no admin key).
"""
import argparse
import sqlite3

from spendguard import config


def _db():
    # the shared ledger file (same one calls/spend_events live in); read-only connection
    path = config.ledger_path() if hasattr(config, "ledger_path") else None
    if not path:
        import os
        path = os.path.join(os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard"), "spend.db")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def diagnose(intent_like=None, min_usd=0.01):
    con = _db()
    cur = con.cursor()
    where = "WHERE intent IS NOT NULL"
    args = []
    if intent_like:
        where += " AND intent LIKE ?"
        args.append(f"%{intent_like}%")
    # per (intent, model, day): the CLEAN calls picture (suspect excluded). Classify by COST (billed), NEVER by
    # output_snip — that column is only populated when prompt/output STORAGE is on, so its absence means "not stored",
    # not "no output" (an earlier version conflated the two and invented a $1.8K 'paid-no-output' artifact). A billed
    # call is cost>0; an ERROR/refusal did not bill (cost 0/NULL); finish='length' is a truncation (a QUALITY note,
    # not a cost cause). errors are surfaced so an "error in the prompt or other" cause is visible, per the ask.
    # suspect rows (part 3a — impossible per-call out_tok) are EXCLUDED from every counted number here (billed_usd,
    # billed_n, err_n, trunc_n); only suspect_n reports how many were set aside. This is what the module docstring claims.
    calls_rows = cur.execute(f"""
        SELECT intent, model, date(ts) day,
          SUM(CASE WHEN cost>0 AND suspect IS NULL THEN cost ELSE 0 END) billed_usd,
          SUM(CASE WHEN cost>0 AND suspect IS NULL THEN 1 ELSE 0 END) billed_n,
          SUM(CASE WHEN (cost IS NULL OR cost=0) AND suspect IS NULL THEN 1 ELSE 0 END) err_n,
          SUM(CASE WHEN finish='length' AND suspect IS NULL THEN 1 ELSE 0 END) trunc_n,
          SUM(CASE WHEN suspect IS NOT NULL THEN 1 ELSE 0 END) suspect_n
        FROM calls {where} GROUP BY intent, model, date(ts)""", args).fetchall()
    tally = {"RECORDING_GAP": [0.0, 0], "ERRORS_ONLY": [0.0, 0], "OK": [0.0, 0], "UNRECONCILED_ESTIMATE": [0.0, 0]}
    findings = []
    for r in calls_rows:
        se = cur.execute("""SELECT SUM(CASE WHEN cost_basis='estimate' THEN realtime_usd ELSE 0 END) est,
                            SUM(CASE WHEN cost_basis!='estimate' OR cost_basis IS NULL THEN realtime_usd ELSE 0 END) billed
                            FROM spend_events WHERE intent=? AND model=? AND day=?""",
                         (r["intent"], r["model"], r["day"])).fetchone()
        se_billed = float(se["billed"] or 0.0)
        se_est = float(se["est"] or 0.0)
        billed = float(r["billed_usd"] or 0.0)
        gap = billed - se_billed
        if billed < min_usd and int(r["err_n"] or 0) > 0:
            cause = "ERRORS_ONLY"                                       # only un-billed attempts (errors/refusals) — no $ to reconcile
        elif billed >= min_usd and se_est > 0 and se_billed <= 0.0:
            cause = "UNRECONCILED_ESTIMATE"                            # an estimate projection that never trued-down to billed
        elif gap >= min_usd:
            cause = "RECORDING_GAP"                                    # ANY material $ gap (absolute floor only — no hidden
            #                                                            relative cutoff that could hide a real gap on a big cell)
        else:
            cause = "OK"
        tally[cause][0] += max(gap, 0.0) if cause != "ERRORS_ONLY" else 0.0
        tally[cause][1] += 1
        if cause in ("RECORDING_GAP", "UNRECONCILED_ESTIMATE"):
            findings.append((cause, r["intent"], r["model"], r["day"], billed, se_billed, se_est,
                             int(r["err_n"] or 0), int(r["trunc_n"] or 0), int(r["suspect_n"] or 0)))
    con.close()
    return tally, findings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reconcile_divergence_diagnose")
    ap.add_argument("--intent", default=None, help="only intents whose name contains this substring")
    ap.add_argument("--min-usd", type=float, default=0.01, help="ignore cells below this $ gap")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args(argv)
    tally, findings = diagnose(a.intent, a.min_usd)
    print("CAUSE tally (calls-vs-spend_events divergence; $ = under-reported success $ where applicable):")
    for cause, (usd, n) in sorted(tally.items(), key=lambda kv: -kv[1][0]):
        print(f"  {cause:<22} cells={n:<5} ${usd:.2f}")
    print(f"\nTop {a.limit} cells needing attention (cause · intent · model · day · calls_billed$ · ledger_billed$ · "
          f"est$ · err_n · trunc_n · suspect_n):")
    findings.sort(key=lambda f: -(f[4] - f[5]))
    for f in findings[:a.limit]:
        cause, intent, model, day, billed, se_billed, se_est, err_n, trunc_n, susp = f
        print(f"  {cause:<22} {(intent or '')[:26]:<26} {(model or '')[:16]:<16} {day} "
              f"calls_billed=${billed:.3f} ledger_billed=${se_billed:.3f} est=${se_est:.3f} "
              f"err={err_n} trunc={trunc_n} suspect={susp}")


if __name__ == "__main__":
    main()
