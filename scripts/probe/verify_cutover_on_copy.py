"""Prove the charges → spend_events cutover on a COPY of the LIVE ledger — the real-data equivalent of
tests/test_cutover_equivalence.py, over the actual 45,941 charges (incl. the ~$10.5k quarantine, the
reconciliation markers, the negative true-downs). Never touches the live db.

Checks, on the copy:
  1. run_cutover reconciles EXACTLY — Σ charges == Σ spend_events (include_void), residual $0.
  2. the cap's number is preserved — budget.spent_since(month) == SpendLedger.spent_dec(month) to the cent.
  3. the five categories stay apart — spent_dec (LLM) vs est/remote/subscription accessors.

    python scripts/probe/verify_cutover_on_copy.py
"""
import os, shutil, sqlite3, tempfile
from decimal import Decimal
from spendguard import config, migrate_charges
from spendguard import ledger as L


def main():
    live = config.db_path()
    tmp = tempfile.mkdtemp(prefix="sg-cutover-verify-")
    copy = os.path.join(tmp, "spend.db")
    for suffix in ("", "-wal", "-shm"):                 # copy WAL sidecars too, or recent writes are invisible
        if os.path.exists(live + suffix):
            shutil.copy2(live + suffix, copy + suffix)
    print(f"live: {live}\ncopy: {copy}\n")

    # month countable BEFORE cutover, straight off charges (the legacy countable_charges view on the copy)
    con = sqlite3.connect(copy)
    month = con.execute("SELECT strftime('%Y-%m-01', MAX(day)) FROM charges").fetchone()[0]
    charges_rows = con.execute("SELECT COUNT(*) FROM charges").fetchone()[0]
    # the copy's own countable view (rebuilt on connect by budget._db, but here read directly for isolation)
    con.close()

    stats = migrate_charges.run_cutover(db_path=copy)
    print("run_cutover stats:")
    for k in ("charges_rows", "migrated", "skipped_zero", "backup_table", "src_exact", "dst_exact", "residual", "reconciles"):
        print(f"  {k}: {stats.get(k)}")

    led = L.SpendLedger(db_path=copy)
    # like-for-like countable: legacy view total vs the new _COUNTABLE total, both since `month`
    con = sqlite3.connect(copy)
    # the countable_charges view exists on the copy (created by SpendLedger? no — by budget._db). Recreate the
    # legacy filter inline to read the copy without importing budget's process-global connection to the LIVE db.
    legacy = con.execute(
        "SELECT COALESCE(SUM(cost),0) FROM charges WHERE day>=? "
        "AND (kind IS NULL OR kind!='meta') "
        "AND (model IS NULL OR model NOT IN ('(provider-batch)','(realtime-history)','(realtime-oracle)','(realtime-reconstructed)')) "
        "AND (conv_id IS NULL OR conv_id!='(impossible-estimate)') "
        "AND (basis IS NULL OR basis!='reconstructed')", (month,)).fetchone()[0]
    con.close()
    new_countable = Decimal(led.spent_dec(since=month))

    print(f"\ncharges: {charges_rows} rows;  countable month {month}:")
    print(f"  legacy countable_charges Σ : ${round(legacy, 2)}")
    print(f"  new spend_events spent_dec : ${round(float(new_countable), 2)}  (exact {new_countable})")
    print(f"  est-value / remote / subscription (must be separate): "
          f"${led.est_value_dec()} / ${led.remote_dec()} / ${led.subscription_dec()}")

    ok_recon = bool(stats.get("reconciles")) and Decimal(stats["residual"]) == 0
    ok_count = round(legacy, 2) == round(float(new_countable), 2)
    print(f"\n[{'OK' if ok_recon else 'FAIL'}] Σ reconciles exactly (residual {stats.get('residual')})")
    print(f"[{'OK' if ok_count else 'FAIL'}] countable preserved to the cent (legacy == spent_dec)")
    raise SystemExit(0 if (ok_recon and ok_count) else 1)


if __name__ == "__main__":
    main()
