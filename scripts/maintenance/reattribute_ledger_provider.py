"""Re-attribute ledger rows stamped with the WRONG provider, and re-price them at the correct vendor's rate.

A model that hits a vendor's OpenAI-COMPATIBLE endpoint gets the SDK's provider (openai) recorded, not the real
vendor — so the row is misattributed and its price can't resolve (the model is not one that provider publishes).
This corrects the provider AND re-prices from the canonical rate (pricing.realtime_cost, never a hardcoded number).

DRY-RUN by default — prints the exact row count, the current cost, the re-priced cost, and the delta. `--apply`
commits, and ONLY after a CONSISTENT snapshot of the ledger (sqlite backup API, never a live db+wal copy).

Default: glm-5.2 recorded under provider='openai' → 'zai' (z.ai's own model; OpenAI never serves it, so the
attribution is unambiguous). Reusable for any (model, wrong-provider → right-provider).

  ./.venv.nosync/bin/python scripts/maintenance/reattribute_ledger_provider.py            # dry-run
  ./.venv.nosync/bin/python scripts/maintenance/reattribute_ledger_provider.py --apply    # commit (snapshots first)
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
from spendguard import config, pricing   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="glm-5.2")
    ap.add_argument("--from-provider", default="openai")
    ap.add_argument("--to-provider", default="zai")
    ap.add_argument("--apply", action="store_true", help="commit the change (a consistent snapshot is taken first)")
    a = ap.parse_args(argv)

    db = config.db_path()
    con = sqlite3.connect(db if a.apply else f"file:{db}?mode=ro", uri=not a.apply)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, in_tok, out_tok, cost FROM calls WHERE model=? AND provider=?",
                       (a.model, a.from_provider)).fetchall()
    if not rows:
        print(f"no rows for model={a.model} provider={a.from_provider} — nothing to do.")
        return 0
    cur_cost = sum(float(r["cost"] or 0.0) for r in rows)
    new_costs = {}
    for r in rows:
        new_costs[r["id"]] = float(pricing.realtime_cost(
            a.model, int(r["in_tok"] or 0), int(r["out_tok"] or 0), provider=a.to_provider) or 0.0)
    new_cost = sum(new_costs.values())

    print(f"model={a.model}   provider {a.from_provider!r} -> {a.to_provider!r}")
    print(f"rows affected        : {len(rows)}")
    print(f"current cost (as {a.from_provider:<8}): ${cur_cost:.4f}")
    print(f"re-priced (as {a.to_provider:<8} rate): ${new_cost:.4f}   ({a.model} = "
          f"${pricing.price(a.model, provider=a.to_provider).get('in_')}/"
          f"${pricing.price(a.model, provider=a.to_provider).get('out')} per 1M)")
    print(f"delta                : ${new_cost - cur_cost:+.4f}")

    if not a.apply:
        print("\nDRY-RUN — pass --apply to commit (a consistent snapshot is taken first).")
        return 0

    bak = f"{db}.bak_reattr_{a.model}_{int(time.time())}"
    snap = sqlite3.connect(bak)
    with snap:
        con.backup(snap)                                   # CONSISTENT snapshot (handles WAL), not a live file copy
    snap.close()
    print(f"\nsnapshot taken: {bak}")
    with con:
        for r in rows:
            con.execute("UPDATE calls SET provider=?, cost=? WHERE id=?",
                        (a.to_provider, new_costs[r["id"]], r["id"]))
    print(f"APPLIED: {len(rows)} row(s) re-attributed to {a.to_provider!r} and re-priced. Rollback: restore {bak}.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
