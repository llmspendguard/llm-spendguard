"""DIAGNOSE (read-only, $0, no admin key) a per-call `calls` vs money-ledger `spend_events` divergence, and PROPERLY
DETERMINE ITS CAUSE before anyone writes the ledger (Ash 2026-09-25: "if there is an error in prompt or other that
could be the cause, we should be able to properly determine this").

The Warden #1 symptom: a metered intent's actuals appear in `calls` but the money ledger under-reports. The cause is
NOT one thing, and copying `calls`->`spend_events` blindly would corrupt attribution. This classifies each (intent,
model, day) cell by CAUSE so the right fix is obvious per cell:

  • RECORDING_GAP   — calls billed $ EXCEEDS what spent_dec represents for the cell (realtime+batch over EVERY basis
                      incl estimate, meta/reconciled/void excluded). The money ledger genuinely under-reports (a
                      non-gated / http-capture-off context). This — and ONLY this — is the reconcile target ($ = the
                      true residual). Sizing the gap vs non-estimate rows instead would DOUBLE-COUNT estimated spend.
  • UNRECONCILED_ESTIMATE — the spend IS represented, but ONLY by a cost_basis='estimate' projection that never
                      trued-down to the billed actual. NOT an under-report ($0 here) — a quality gap; reconciling it
                      would double-count the estimate. Surfaced so a stuck true-down is visible.
  • ERRORS_ONLY     — only un-billed attempts (errors/refusals, cost 0/NULL) — the "error in prompt or other" cells,
                      surfaced so a prompt/again-billed bug is caught; no $ to reconcile.
  • SUSPECT_EXCLUDED — `calls` rows flagged suspect (impossible per-call out_tok) are excluded from every number here.
  • OK              — calls success $ ~matches what spent_dec represents (no material gap).

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
        # `represented` is what spent_dec ACTUALLY sees for the cell — it MIRRORS SpendLedger._COUNTABLE: realtime+batch
        # over billed AND estimate (spent_dec counts estimates) and any prior billed fill, but EXCLUDING meta /
        # reconciled / void / reversed AND cost_basis='reconstructed' (plan-covered Claude Code usage — a different kind
        # of spend, kept out of the real-$ headline). Comparing calls against `est_only`/`billed_actual` alone
        # MISCLASSIFIES: an estimate that stands in for the spend is NOT an under-report, and calling it a RECORDING_GAP
        # would double-count if reconciled. `billed_actual` is tracked only to name the estimate-not-trued-down case.
        se = cur.execute("""SELECT
              COALESCE(SUM(CASE WHEN COALESCE(is_meta,0)=0 AND COALESCE(reconciled,0)=0
                    AND COALESCE(status,'') NOT IN ('void','reversed') AND COALESCE(cost_basis,'') != 'reconstructed'
                    THEN COALESCE(realtime_usd,0)+COALESCE(batch_usd,0) ELSE 0 END),0) represented,
              COALESCE(SUM(CASE WHEN cost_basis='estimate' THEN COALESCE(realtime_usd,0)+COALESCE(batch_usd,0) ELSE 0 END),0) est,
              COALESCE(SUM(CASE WHEN COALESCE(cost_basis,'') NOT IN ('estimate','reconstructed')
                    AND COALESCE(is_meta,0)=0 AND COALESCE(reconciled,0)=0
                    THEN COALESCE(realtime_usd,0)+COALESCE(batch_usd,0) ELSE 0 END),0) billed_actual
              FROM spend_events WHERE intent=? AND model=? AND day=?""",
                         (r["intent"], r["model"], r["day"])).fetchone()
        represented = float(se["represented"] or 0.0)
        se_est = float(se["est"] or 0.0)
        billed_actual = float(se["billed_actual"] or 0.0)
        billed = float(r["billed_usd"] or 0.0)
        gap = billed - represented                                     # TRUE under-report of spent_dec (incl estimate)
        if billed < min_usd and int(r["err_n"] or 0) > 0:
            cause = "ERRORS_ONLY"                                       # only un-billed attempts (errors/refusals) — no $ to reconcile
        elif gap >= min_usd:
            cause = "RECORDING_GAP"                                    # spent_dec genuinely under-reports — the reconcile
            #                                                            target (absolute floor only, no hidden relative cutoff)
        elif se_est >= min_usd and billed_actual < min_usd:
            cause = "UNRECONCILED_ESTIMATE"                            # COVERED by an estimate projection that never trued-down
            #                                                            to the billed actual: NOT an under-report ($0), a quality gap
        else:
            cause = "OK"
        tally[cause][0] += max(gap, 0.0) if cause == "RECORDING_GAP" else 0.0
        tally[cause][1] += 1
        if cause in ("RECORDING_GAP", "UNRECONCILED_ESTIMATE"):
            findings.append((cause, r["intent"], r["model"], r["day"], billed, represented, se_est,
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
    print(f"\nTop {a.limit} cells needing attention (cause · intent · model · day · calls_billed$ · represented$ · "
          f"est$ · err_n · trunc_n · suspect_n):")
    findings.sort(key=lambda f: -(f[4] - f[5]))
    for f in findings[:a.limit]:
        cause, intent, model, day, billed, represented, se_est, err_n, trunc_n, susp = f
        print(f"  {cause:<22} {(intent or '')[:26]:<26} {(model or '')[:16]:<16} {day} "
              f"calls_billed=${billed:.3f} represented=${represented:.3f} est=${se_est:.3f} "
              f"err={err_n} trunc={trunc_n} suspect={susp}")


if __name__ == "__main__":
    main()
