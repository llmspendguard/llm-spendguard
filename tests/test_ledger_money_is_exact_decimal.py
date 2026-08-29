"""spend_events money is EXACT DECIMAL — not integer micros (truncates), not binary float (drifts).

WHY THIS EXISTS. The ledger stored money as integer micro-USD (`int(round(usd*1e6))`). A real call costing
$0.00000026 rounded to 0 micros, and `SpendLedger.record_event` then raised "no cost in any micros column" — so
`migrate_charges` (marked "done", task #67) crashed on the first sub-micro row of the real data and never
ran to completion (finding F7). Owner decision: money is Decimal (exact), stored as TEXT + Python Decimal,
summed with the registered `dec_sum` aggregate. This test is the guard that keeps it that way.

Each check states the input that fails under the OLD representation and passes under Decimal.
"""
import pathlib
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import ledger  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _fresh():
    d = tempfile.mkdtemp()
    return ledger.SpendLedger(db_path=str(pathlib.Path(d) / "t.db"))


print("-- the exact value that crashed the migration is now preserved --")
L = _fresh()
# $0.00000026 = 0.26 micros -> truncated to 0 under the old code -> record_event() raised. Must record now.
try:
    L.record_event({"kind": "realtime", "usd": "0.00000026", "provider": "anthropic", "model": "claude-haiku-4-5",
              "source": "test", "dedup_key": "submicro-1"})
    crashed = False
except Exception as e:
    crashed = True
    detail = f"{type(e).__name__}: {e}"
check("a $0.00000026 realtime call records without crashing", not crashed,
      detail if crashed else "")
if not crashed:
    got = L.sum_usd(source="test")
    check("and it round-trips EXACTLY (not truncated to $0)", Decimal(str(got)) == Decimal("0.00000026"),
          f"sum_usd returned {got!r}, expected 0.00000026")

print("\n-- summation is exact: no binary-float drift --")
L2 = _fresh()
# The textbook float failure: 0.1 + 0.2 != 0.3. Ten dimes + ... must total exactly.
for i, amt in enumerate(["0.1", "0.2", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1", "0.1"]):
    L2.record_event({"kind": "realtime", "usd": amt, "provider": "openai", "model": "gpt-5.5",
               "source": "drift", "dedup_key": f"d{i}"})
total = L2.sum_usd(source="drift")
check("0.1+0.2+0.1*8 sums to EXACTLY 1.10 (float would give 1.0999999999999999)",
      Decimal(str(total)) == Decimal("1.10"), f"got {total!r}")

print("\n-- a genuinely missing cost is still rejected (the guard still guards) --")
L3 = _fresh()
try:
    L3.record_event({"provider": "openai", "model": "gpt-5.5", "source": "x", "dedup_key": "nocost"})  # no kind/usd
    rejected = False
except ValueError:
    rejected = True
check("an event with no money at all is refused", rejected,
      "a truly cost-less event must raise — the guard catches a MISSING cost, only sub-micro reals pass")

print("\n-- rollup keeps the five categories separate and exact --")
L4 = _fresh()
L4.record_event({"kind": "batch", "usd": "1.234567", "provider": "openai", "model": "gpt-5.5", "source": "r", "dedup_key": "b"})
L4.record_event({"kind": "realtime", "usd": "2.765433", "provider": "openai", "model": "gpt-5.5", "source": "r", "dedup_key": "rt"})
roll = L4.rollup(where={"source": "r"})
check("batch and realtime land in their own columns", roll["batch_usd"] == 1.234567 and roll["realtime_usd"] == 2.765433,
      f"batch={roll.get('batch_usd')} realtime={roll.get('realtime_usd')}")
check("billed_usd is the exact sum across billed categories",
      Decimal(str(roll["billed_usd"])) == Decimal("4.0"), f"billed_usd={roll.get('billed_usd')}")

print(("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails)))
sys.exit(1 if _fails else 0)
