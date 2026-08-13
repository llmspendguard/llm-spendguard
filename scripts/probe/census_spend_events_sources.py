"""Cutover grounding probe: is `charges` the COMPLETE source for `spend_events`, or does the live ledger
already hold rows from other paths (forward-capture, direct records) that a charges-only rebuild would lose?

Answering this decides whether `run_cutover` may rebuild spend_events purely from charges, or must preserve
non-charges rows. Read-only; prints a census by source + schema_version. Zero spend.

    python scripts/probe/census_spend_events_sources.py
"""
import sqlite3
from spendguard import config


def main():
    path = config.db_path()
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    print(f"db: {path}")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone():
        print("  (no spend_events table yet)")
        return
    n = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
    print(f"spend_events rows: {n}")
    print("by source:")
    for r in con.execute("SELECT COALESCE(source,'(none)') s, COUNT(*) n FROM spend_events GROUP BY 1 ORDER BY n DESC"):
        print(f"  {r['s']:>24}  n={r['n']}")
    print("by schema_version:")
    for r in con.execute("SELECT COALESCE(schema_version,0) v, COUNT(*) n FROM spend_events GROUP BY 1 ORDER BY 1"):
        print(f"  v{r['v']}: {r['n']}")
    non = con.execute("SELECT COUNT(*) FROM spend_events WHERE COALESCE(source,'') NOT LIKE 'migrate:charges%'").fetchone()[0]
    print(f"rows NOT from migrate:charges (a charges-only rebuild would LOSE these): {non}")
    con.close()


if __name__ == "__main__":
    main()
