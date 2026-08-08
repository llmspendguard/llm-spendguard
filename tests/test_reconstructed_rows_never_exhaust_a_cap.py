"""A RECONSTRUCTION restates history. It must never be able to exhaust a live period budget.

WHY THIS GUARD EXISTS. Realtime reconstruction wrote a single charge of $10,409.24 — covering MONTHS of
recovered spend — with TODAY's date and basis 'billed'. Every period cap then evaluated a $10,480 month
against $70 of actual API spend, and the gate refused every call for the rest of the day. The money was real
and was already counted in the period it was actually spent in; summing it again here double-counts, and the
cap fires on dollars that did not move.

This is the third time today a guard blocked real work over money nobody spent this period: phantom charges
from reading batch history, and now a backfill dated today. The pattern is always the same shape — a row that
means something other than "money spent in this window" being summed as if it did — so the fix is the same
shape too: the row's own `basis` says what kind of number it is, and the reader honours it.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-recon-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import datetime                                             # noqa: E402

from spendguard import budget, config                       # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

budget.record("openai", "gpt-5.5", "realtime", 5.00, basis=budget.BASIS_BILLED)
base = budget.spent_since(today)
check("billed spend counts toward the period", base >= 5.00, str(base))

budget.record("openai", "(realtime-history)", "realtime", 10_409.24, basis=budget.BASIS_RECONSTRUCTED)
after = budget.spent_since(today)
check("a RECONSTRUCTED backfill does NOT count toward the period", after == base, f"{base} -> {after}")

check("...so it cannot exhaust a cap either",
      budget.exceeded(1.0) is None or budget.exceeded(1.0)[2] < 100,
      str(budget.exceeded(1.0)))

budget.record("openai", "gpt-5.5", "realtime", 2.50, basis=budget.BASIS_ESTIMATE)
check("an ESTIMATE still counts — it is the pre-spend guard and must bind",
      budget.spent_since(today) > after, str(budget.spent_since(today)))

# The three markers that must all stay out, for three different reasons.
import sqlite3                                              # noqa: E402
c = sqlite3.connect(config.db_path())
before = budget.spent_since(today)
c.execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id,key_fp,basis) "
          "VALUES (?,?,?,?,?,?,?,?,?,?)",
          (today + "T00:00:00+00:00", today, "openai", "gpt-5.5", "realtime", 999.0, "",
           budget.QUARANTINE_CONV, "", budget.BASIS_ESTIMATE))
c.commit()
check("a QUARANTINED impossible estimate stays out", budget.spent_since(today) == before,
      str(budget.spent_since(today)))

print(f"\n{'[FAIL]' if failures else 'OK'} test_reconstructed_rows_never_exhaust_a_cap: {failures} failure(s)")
sys.exit(1 if failures else 0)
