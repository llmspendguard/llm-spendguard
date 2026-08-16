"""Re-reading an already-billed batch must not be recorded as new spend, and must not trip a cap.

WHY THIS GUARD EXISTS. Recovering a past batch means downloading its INPUT file and parsing it. Those bytes
look exactly like a batch about to be submitted, so the gate estimated them as new spend. One read-only,
zero-token `callio.fetch_history()` pass wrote $359.63 of charges for work billed months earlier (plus
$10,050 more that the impossibility rail caught), and the daily cap then refused EVERY genuine call for the
rest of the day — the failure was not a lost dollar, it was a guard confidently blocking real work over money
nobody spent. A guard that does that teaches you to switch it off, which costs far more than it saves.

The invariant: recording is for money that MOVED. A projection about bytes you are merely reading is not that.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-readhist-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import inspect                                          # noqa: E402

from spendguard import budget, callio           # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def _today_total():
    # money-of-record is spend_events now; sum everything booked today (all categories, incl meta/void — this
    # test only cares WHETHER a row was booked, not the countable filter)
    from spendguard import ledger as _L
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        return float(_L.SpendLedger().sum_dec(since=today, include_void=True))
    except Exception:
        return 0.0


budget.record("openai", "gpt-5.5", "realtime", 1.25, basis="billed")
base = _today_total()
check("a normal charge IS recorded", base >= 1.25, str(base))

with budget.reading_history("probe"):
    budget.record("openai", "gpt-5.5", "realtime", 999.0, basis="estimate")
check("a charge inside reading_history is NOT recorded", _today_total() == base, str(_today_total()))

check("the flag is off by default", budget.is_reading_history() is False)
with budget.reading_history("probe"):
    check("...on inside the block", budget.is_reading_history() is True)
check("...and restored on exit", budget.is_reading_history() is False)

budget.record("openai", "gpt-5.5", "realtime", 0.50, basis="billed")
check("recording resumes after the block", _today_total() > base, str(_today_total()))

# The context is worthless if the one caller that caused the bug does not use it.
src = inspect.getsource(callio.fetch_history)
check("callio.fetch_history runs inside reading_history()", "reading_history(" in src)

from spendguard import gate                             # noqa: E402
check("the cap check stands down while reading history",
      "is_reading_history()" in inspect.getsource(gate._cap_gate)
      if hasattr(gate, "_cap_gate") else
      any("is_reading_history()" in inspect.getsource(f)
          for f in (getattr(gate, n) for n in dir(gate))
          if callable(f) and getattr(f, "__module__", "") == gate.__name__))

print(f"\n{'[FAIL]' if failures else 'OK'} test_reading_history_is_not_spending: {failures} failure(s)")
sys.exit(1 if failures else 0)
