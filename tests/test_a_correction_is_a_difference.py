"""Correcting a locked entry posts the DIFFERENCE, and the tamper log knows who wrote around it.

FOUND BY WAVE 2 of the multi-vendor review — kimi-k3 and gpt-5.5 independently, both validators confirming.
The second half was found by the guard for the first half, in code written the same afternoon.

A CORRECTION THAT INFLATES
  `adjust(eid, changes)` cloned the original event's amounts and let `changes` replace some of them, then
  posted that as a new row — while the ORIGINAL row stays, which is the entire point of a locked period.
  MEASURED: an event of realtime 1,000,000 / batch 250,000, adjusted to realtime 400,000, summed to
  realtime 1,400,000 and batch 500,000. It raised the figure it was called to lower, and doubled a column
  nobody had touched. `reverse()` had it right all along — it negates every column, so the pair sums to
  zero. `adjust()` was the same idea with the arithmetic left out.

AND REVERSING AN ALREADY-ADJUSTED EVENT
  produced MINUS 600,000 — a total for money that was never spent — because the reversal negates the
  ORIGINAL and leaves the deltas standing. There is no obviously-right answer, so it refuses and names
  the entries in the way rather than picking one and being quietly wrong about somebody's books.

A TAMPER LOG THAT CANNOT SAY WHICH KIND OF BROKEN IT IS
  budget.reattribute_providers and budget.quarantine_charge both INSERTed straight into spend_audit,
  leaving 796 rows with no row_hash. Chaining onto that None raised TypeError, so ONE out-of-band row
  disabled auditing for every write after it — and verify_audit_chain reported the result as a broken
  chain, which is what tampering looks like. Our own bug and someone editing the books read identically.

  The hashes are NOT recomputed to make the chain look whole. A tamper-evidence record that repairs
  itself is not evidence of anything.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-correction-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard.ledger import SpendLedger                                    # noqa: E402

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


L = SpendLedger()
total = lambda c: L._conn.execute(f"SELECT COALESCE(SUM({c}),0) FROM spend_events").fetchone()[0]

print("  a correction posts the difference:")
e = L.record({"source": "guard-a", "provider": "openai", "model": "m", "kind": "realtime",
              "realtime_micros": 1_000_000, "batch_micros": 250_000})
L.adjust(e, {"realtime_micros": 400_000}, actor="guard", reason="the real figure was 0.40")
check("the corrected column SUMS to the intended value", total("realtime_micros") == 400_000,
      f"{total('realtime_micros'):,} — a correction that inflates is worse than none")
check("a column the correction did not mention is unchanged, not doubled",
      total("batch_micros") == 250_000, f"{total('batch_micros'):,}")
check("the original row is untouched — the point of a locked period",
      L._conn.execute("SELECT COUNT(*) FROM spend_events WHERE id=?", (e,)).fetchone()[0] == 1)

try:
    L.reverse(e, actor="guard", reason="should be refused")
    check("reversing an ADJUSTED event is refused, not silently wrong", False, "it was allowed")
except ValueError as err:
    check("reversing an ADJUSTED event is refused, not silently wrong", True)
    check("...and the refusal names what is in the way", "adjustment" in str(err), str(err)[:70])

e2 = L.record({"source": "guard-b", "provider": "openai", "model": "m2", "kind": "realtime",
               "realtime_micros": 500_000})
before = total("realtime_micros")
L.reverse(e2, actor="guard", reason="clean")
check("a clean reversal still zeroes its own event",
      total("realtime_micros") == before - 500_000, f"{total('realtime_micros'):,}")

print("\n  the tamper log distinguishes bypassed from edited:")
d = L.verify_audit_chain(detail=True)
check("a chain written only through audit() verifies OK", d["ok"] and d["n_unchained"] == 0, str(d))

L._conn.execute("INSERT INTO spend_audit (event_id, actor, pass, field, reason) VALUES (?,?,?,?,?)",
                ("bypass", "someone", "raw", "f", "written around the chain"))
L._conn.commit()
d = L.verify_audit_chain(detail=True)
check("a row written AROUND the chain reports as unchained, NOT as tampering",
      d["n_unchained"] == 1 and d["n_tampered"] == 0, str(d))
L.audit("after-bypass", "guard", "t", "f", None, "v", "one bypass must not disable auditing forever")
check("...and writes still work after it, instead of raising forever",
      L._conn.execute("SELECT COUNT(*) FROM spend_audit WHERE event_id='after-bypass'").fetchone()[0] == 1)

# EDITING a row is the event this table exists to catch, and it must still be caught.
rid = L._conn.execute("SELECT id FROM spend_audit WHERE row_hash IS NOT NULL ORDER BY id LIMIT 1").fetchone()[0]
L._conn.execute("UPDATE spend_audit SET reason='quietly changed' WHERE id=?", (rid,))
L._conn.commit()
d = L.verify_audit_chain(detail=True)
check("an EDITED row is reported as tampering", d["n_tampered"] >= 1 and not d["ok"], str(d))

print(f"\n{'[FAIL]' if failures else 'OK'} test_a_correction_is_a_difference: {failures} failure(s)")
sys.exit(1 if failures else 0)
