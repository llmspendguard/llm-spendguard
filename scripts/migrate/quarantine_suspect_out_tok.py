"""Quarantine (FLAG, never delete) existing per-call `calls` rows whose out_tok is physically impossible for ONE call
— out_tok > 2x the model's output ceiling. These are backfill/aggregate artifacts (measured 2026-09-25: 1M-68M out_tok
recorded under gpt-5-nano, whose ceiling is 128K; caller=backfill:ledger / spendguard:<module>) that pollute per-call
analysis and made the estimate-vs-actual reconciliation untrustworthy. This sets calls.suspect with the reason; the raw
out_tok/cost are PRESERVED (never clamped or deleted — raw-data preservation). Idempotent (skips already-flagged rows).

Run under the gate:  ./.venv.nosync/bin/python scripts/migrate/quarantine_suspect_out_tok.py
The `calls` table is per-call TELEMETRY (not the money ledger spend_events); this only marks rows, and the flag is
reversible (UPDATE calls SET suspect=NULL WHERE …). Nothing in spend_events is touched.
"""
import os

os.environ.setdefault("SPENDGUARD_CALLS", "1")   # ensure the calls schema is materialised (adds the suspect column)
from spendguard import calls, model_catalog as mc  # noqa: E402


def main():
    db = calls._calls_db()                       # pooled ledger conn; _ensure_calls_schema has added `suspect`
    with calls._lock:
        models = [r[0] for r in db.execute(
            "SELECT DISTINCT model FROM calls WHERE out_tok IS NOT NULL AND model IS NOT NULL").fetchall()]
        total = 0
        factor = calls._SUSPECT_CEILING_FACTOR   # the ONE bound (shared with record_call's live guard)
        for model in models:
            ceil = mc.published_ceiling(model)
            if not ceil:
                continue                         # unknown ceiling → never flag (conservative; no guessed bound)
            bound = int(int(ceil) * factor)
            cur = db.execute(
                "UPDATE calls SET suspect = ? WHERE model = ? AND out_tok > ? AND suspect IS NULL",
                (f"out_tok > {factor}x ceiling {int(ceil)} (impossible per-call: aggregate/backfill/bug)", model, bound))
            if cur.rowcount:
                print(f"  flagged {cur.rowcount:>4} suspect row(s) for {model} (out_tok > {bound:,})")
                total += cur.rowcount
        db.commit()
    print(f"quarantine complete: {total} row(s) flagged suspect (raw out_tok/cost preserved; reversible).")


if __name__ == "__main__":
    main()
