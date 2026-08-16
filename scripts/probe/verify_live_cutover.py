"""Post-cutover LIVE verification (read-only): after `spendguard migrate` rebuilt spend_events, confirm

  1. `charges` (still the cap's source until the repoint) is INTACT — the countable month total is unchanged,
  2. the new v6 `spend_events` gives the SAME countable to the cent (budget.spent_since == SpendLedger.spent_dec),
  3. the safety net is in place — the `spend_events_precutover` backup table + the whole-file snapshot exist.

Never writes. Zero spend.

    python scripts/probe/verify_live_cutover.py
"""
import sqlite3, glob
from decimal import Decimal
from spendguard import config, budget
from spendguard import ledger as L


def main():
    path = config.db_path()
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    se_cols = {r[1] for r in con.execute("PRAGMA table_info(spend_events)")}
    n_charges = con.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
    n_se = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    n_backup = con.execute("SELECT COUNT(*) FROM spend_events_precutover").fetchone()[0] if "spend_events_precutover" in tables else None
    con.close()

    month = budget._utc().strftime("%Y-%m-01")
    charges_month = budget.spent_since(month)                 # reads countable_charges (the cap's current source)
    se_month = Decimal(L.SpendLedger().spent_dec(since=month))  # reads the new v6 spend_events (_COUNTABLE, LLM only)
    snaps = sorted(glob.glob(str(config.HOME / "snapshots" / "spend-*-cutover.db")))

    print(f"db: {path}")
    print(f"charges rows: {n_charges}")
    print(f"spend_events rows: {n_se}  (v6? {'batch_usd' in se_cols and 'intent' in se_cols})")
    print(f"backup table spend_events_precutover rows: {n_backup}")
    print(f"cutover snapshots: {len(snaps)}  latest={snaps[-1] if snaps else None}")
    print(f"countable month {month}:  charges spent_since = ${round(charges_month,2)}   spend_events spent_dec = ${round(float(se_month),2)}")

    ok_v6 = ("batch_usd" in se_cols) and ("intent" in se_cols)
    ok_backup = bool(n_backup) and bool(snaps)
    ok_match = round(charges_month, 2) == round(float(se_month), 2)
    print(f"\n[{'OK' if ok_v6 else 'FAIL'}] spend_events is v6 (has usd + forensic columns)")
    print(f"[{'OK' if ok_backup else 'FAIL'}] recovery net present (backup table + file snapshot)")
    print(f"[{'OK' if ok_match else 'FAIL'}] countable preserved: charges == spend_events to the cent")
    raise SystemExit(0 if (ok_v6 and ok_backup and ok_match) else 1)


if __name__ == "__main__":
    main()
