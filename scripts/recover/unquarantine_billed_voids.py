#!/usr/bin/env python3
"""Recover spend_events rows that were WRONGLY quarantined — real, provider-BILLED calls voided as "impossible".

The batched-embedding bug (fixed in gate._implausible_estimate / _embed_per_item_max) checked a batched
embeddings call's tokens as sum/1_request against the context window, declared it IMPOSSIBLE, and VOIDED the
row — even though each item fit the window and the provider ACCEPTED and BILLED the request. A voided row is
excluded from every total, so the ledger UNDER-counts the real spend by the sum of these rows.

The safe-to-recover class is PRINCIPLED, not model-specific: status='void' AND cost_basis='billed' AND a real
cost > 0. A cost that came from the provider's OWN usage means the request was accepted, so it cannot have been
an impossible estimate. A cost_basis='estimate' void is a pre-submission projection the provider never accepted
(e.g. the base64-as-tokens batch bug) — those are genuinely impossible and stay quarantined.

Selection is by fixed-shape ledger fields (status/cost_basis/usd) — parsing, not judgement. Recovery goes
through budget.unquarantine_charge (audited hash-chain update, idempotent). DRY-RUN by default; --apply mutates.

  python scripts/recover/unquarantine_billed_voids.py               # dry-run: show what would be recovered
  python scripts/recover/unquarantine_billed_voids.py --apply       # recover them
  python scripts/recover/unquarantine_billed_voids.py --model-like text-embedding --since 2026-08-01 --apply
"""
import argparse
import sqlite3
from collections import defaultdict

from spendguard import config, budget
from spendguard.ledger import to_dec


def _money(row):
    return float(to_dec(row["realtime_usd"]) + to_dec(row["batch_usd"]))


def select_recoverable(db_path, model_like=None, since=None):
    """Rows the provider ACCEPTED but the gate voided: status='void', cost_basis='billed', cost>0.
    model_like / since are OPTIONAL scoping filters (SQL LIKE on model, day >= since) — never required."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    where = ["COALESCE(status,'')='void'", "COALESCE(cost_basis,'')='billed'"]
    args = []
    if model_like:
        where.append("model LIKE ?")
        args.append(f"%{model_like}%")
    if since:
        where.append("day >= ?")
        args.append(since)
    sql = ("SELECT id, ts_utc, day, provider, model, cost_basis, conv_id, realtime_usd, batch_usd, actor "
           "FROM spend_events WHERE " + " AND ".join(where) + " ORDER BY ts_utc")
    rows = [r for r in con.execute(sql, args).fetchall() if _money(r) > 0]   # cost>0: a real billed charge
    con.close()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="mutate the ledger (default is a dry-run report)")
    ap.add_argument("--model-like", default=None, help="optional: only rows whose model matches this substring")
    ap.add_argument("--since", default=None, help="optional: only rows on/after this day (YYYY-MM-DD)")
    args = ap.parse_args()

    db_path = config.db_path()
    rows = select_recoverable(db_path, model_like=args.model_like, since=args.since)
    total = sum(_money(r) for r in rows)
    print(f"ledger: {db_path}")
    print(f"recoverable (void + billed + cost>0): {len(rows)} row(s) · ${total:.4f}")
    by_model = defaultdict(lambda: [0, 0.0])
    by_src = defaultdict(lambda: [0, 0.0])
    for r in rows:
        by_model[r["model"]][0] += 1; by_model[r["model"]][1] += _money(r)
        by_src[(r["actor"] or "")[:48]][0] += 1; by_src[(r["actor"] or "")[:48]][1] += _money(r)
    print("  by model:")
    for m, (c, s) in sorted(by_model.items(), key=lambda x: -x[1][1]):
        print(f"    {m:30} {c:4} row(s)  ${s:.4f}")
    print("  by source:")
    for s_, (c, s) in sorted(by_src.items(), key=lambda x: -x[1][1]):
        print(f"    {s_:50} {c:4} row(s)  ${s:.4f}")

    if not rows:
        print("nothing to recover.")
        return
    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to recover these rows.")
        return

    since_day = min(r["day"] for r in rows)
    before = budget.spent_since(since_day)
    n = 0
    for r in rows:
        n += budget.unquarantine_charge(row=r["id"], reason="recovered: batched-embedding false quarantine")
    after = budget.spent_since(since_day)
    print(f"\nRECOVERED {n} row(s). spent_since({since_day}): ${before:.4f} → ${after:.4f}  (+${after - before:.4f})")
    print("verify: `spendguard quarantine --list` should no longer show them; `spendguard receipt` totals include them.")


if __name__ == "__main__":
    main()
