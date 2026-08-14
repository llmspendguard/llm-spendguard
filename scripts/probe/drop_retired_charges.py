"""Step 6b final: DROP the retired `charges` table + `countable_charges` view from the live ledger, now that
spend_events is the single money-of-record and no code reads or writes charges.

Safe by construction:
  • snapshots the WHOLE db file first (budget.snapshot — the tested recovery path),
  • proves the cap number is unchanged across the drop (charges was already inert),
  • keeps the spend_events_precutover backup table (the pre-cutover rows stay recoverable),
  • idempotent (DROP ... IF EXISTS).

    python scripts/probe/drop_retired_charges.py
"""
import sqlite3
from spendguard import budget, config


def main():
    path = config.db_path()
    con = sqlite3.connect(path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    print(f"db: {path}")
    print(f"charges present: {'charges' in tables}   countable_charges view: {'countable_charges' in tables}")

    se_before = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    month = budget._utc().strftime("%Y-%m-01")
    cap_before = budget.spent_since(month)     # reads spend_events (the money-of-record) — must be unchanged
    con.close()

    snap = budget.snapshot(reason="pre-drop-charges")
    print(f"snapshot: {snap}")
    if not snap:
        print("REFUSED: could not snapshot the db before a destructive drop.")
        raise SystemExit(1)

    con = sqlite3.connect(path)
    con.execute("DROP VIEW IF EXISTS countable_charges")
    con.execute("DROP TABLE IF EXISTS charges")
    con.commit()
    tables2 = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    se_after = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    backup_present = "spend_events_precutover" in tables2
    con.close()
    budget._LEDGER = None                       # drop any cached ledger handle so the next read reconnects clean
    budget._conn = None
    cap_after = budget.spent_since(month)

    print(f"\ncharges gone: {'charges' not in tables2}   countable_charges gone: {'countable_charges' not in tables2}")
    print(f"spend_events rows: {se_before} -> {se_after}  (unchanged: {se_before == se_after})")
    print(f"pre-cutover backup kept: {backup_present}")
    print(f"cap (month {month}): ${round(cap_before, 2)} -> ${round(cap_after, 2)}  (unchanged: {round(cap_before,2)==round(cap_after,2)})")

    ok = ("charges" not in tables2 and "countable_charges" not in tables2
          and se_before == se_after and round(cap_before, 2) == round(cap_after, 2) and backup_present)
    print(f"\n[{'OK' if ok else 'FAIL'}] charges retired; spend_events intact; cap unchanged; backup kept")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
