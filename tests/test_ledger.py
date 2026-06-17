"""Offline test for the local ledger helpers (by_day / ledger_start / exceeded) — isolated home."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ledger-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


# record workload (batch + realtime) and meta charges
budget.record("openai", "gpt-5.5", "batch", 100.0)
budget.record("openai", "gpt-5.5", "realtime", 5.0)
budget.record_meta("anthropic", "claude-opus-4-8", 2.0)

print("-- by_day / kind filters --")
day = budget._utc().strftime("%Y-%m-%d")
check("batch by_day", abs(budget.by_day(kind="batch").get(day, 0) - 100.0) < 1e-9)
check("realtime by_day", abs(budget.by_day(kind="realtime").get(day, 0) - 5.0) < 1e-9)
check("exclude_meta sums workload only (105, not 107)",
      abs(budget.by_day(exclude_meta=True).get(day, 0) - 105.0) < 1e-9)
check("meta by_day", abs(budget.by_day(kind="meta").get(day, 0) - 2.0) < 1e-9)
check("ledger_start = today", budget.ledger_start() == day)

print("-- caps: workload excludes meta; meta has its own cap --")
# spent_today (workload) = 105, NOT 107 — meta is segregated
check("workload spent_today excludes meta", abs(budget.spent_today() - 105.0) < 1e-9)
check("$50 more → under no cap (none set)", budget.exceeded(50.0) is None)
print("done.")
