#!/usr/bin/env python
"""Migrate the legacy `charges` ledger → the financial-grade `spend_events` (SpendLedger). Thin CLI over
`spendguard.migrate_charges`; ADDITIVE + IDEMPOTENT (never touches charges, re-runs book nothing new).

  (default) estimate : count + $ that WOULD migrate, and what's already there — ZERO writes
  run                : perform the migration + verify Σ conservation + the audit chain

Run UNDER the gated venv (`.venv/bin/python`). No LLM spend (attribution reuses recorded classifications + taxonomy).
"""
import sys, sqlite3
import spendguard; spendguard.require()
from spendguard import migrate_charges, config
from spendguard import ledger as L


def _estimate():
    src = sqlite3.connect(config.db_path()); src.row_factory = sqlite3.Row
    n = src.execute("SELECT COUNT(*) FROM charges WHERE cost != 0").fetchone()[0]
    tot = src.execute("SELECT COALESCE(SUM(cost),0) FROM charges").fetchone()[0]
    already = L.SpendLedger().sum_usd(source="migrate:charges")
    print("ESTIMATE: %d non-zero charge rows → spend_events" % n)
    print("  charges total $%.2f  |  already migrated $%.2f" % (tot, already))
    print("  zero writes. Run `charges_to_spend_events.py run` to migrate (idempotent, Σ-verified).")


def _run():
    st = migrate_charges.to_spend_events()
    print("MIGRATED: %d rows booked (%d zero skipped, of %d charges)"
          % (st["migrated"], st["skipped_zero"], st["charges_rows"]))
    print("  src $%.2f  →  spend_events(migrate:charges) $%.2f  |  delta $%.6f"
          % (st["src_total_usd"], st["dst_total_usd"], st["delta_usd"]))
    conserved = abs(st["delta_usd"]) < 0.01
    chain_ok, bad = L.SpendLedger().verify_audit_chain()
    print("  Σ check: %s   audit chain: %s"
          % ("PASS (every dollar conserved)" if conserved else "FAIL — investigate",
             "intact" if chain_ok else f"ALTERED at audit id {bad}"))
    sys.exit(0 if conserved and chain_ok else 1)


if __name__ == "__main__":
    (_run if (len(sys.argv) > 1 and sys.argv[1] == "run") else _estimate)()
