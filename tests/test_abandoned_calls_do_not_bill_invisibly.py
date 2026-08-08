"""A call the caller stopped waiting for must be CANCELLED, and every attempt must be counted.

WHY THIS GUARD EXISTS — the worst find of the project, in the module built to make calls safe.
A 10-file review reported spending $1.98. The ledger recorded $13.12. The caller saw 18 results; 146 calls
were billed. Two independent defects, and each alone makes a budget unenforceable:

  1. `_attempt` ran the adapter on a daemon thread and did `t.join(timeout=budget_s)`. join(timeout=) stops
     WAITING; it does not cancel. Every abandoned request ran to completion and billed, and the caller never
     learned it existed — so the spend was invisible by construction, not by accident.
  2. `call()` retries, and returned only the LAST attempt's cost. Retried attempts billed and were never
     counted.

Both are the same failure in different clothes: money moved, and the number the caller reads did not.
That is the exact thing this whole project exists to prevent, and it was in our own call layer.
"""
import inspect
import sys

from spendguard import adapters, vendor_call as vc

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_the_sdk_gets_a_real_timeout_so_abandonment_cancels():
    check("adapters.call accepts timeout_s", "timeout_s" in inspect.signature(adapters.call).parameters)
    src = inspect.getsource(adapters.call)
    check("both SDK clients are constructed WITH it — a client without a timeout runs to completion",
          src.count("timeout=timeout_s") >= 2, str(src.count("timeout=timeout_s")))
    check("_attempt hands the SDK the same budget the caller is enforcing",
          "timeout_s=budget_s" in inspect.getsource(vc._attempt),
          "otherwise the caller's deadline and the request's lifetime are unrelated")


def test_every_attempt_is_counted_not_just_the_last():
    """A retry is a second billed call. Reporting only the final one understates by exactly the retries."""
    costs = iter([0.10, 0.20, 0.30])
    calls = {"n": 0}

    def fake_attempt(*a, **k):
        calls["n"] += 1
        c = next(costs)
        # transport_error on the first two so the retry path runs, then success
        if calls["n"] < 3:
            return {"error": "Error code: 503 - overloaded", "text": None, "cost": c}
        return {"text": "hi", "out_tok": 5, "finish_reason": "end_turn", "cost": c}

    orig = vc._attempt
    try:
        vc._attempt = fake_attempt
        r = vc.call("anthropic", "claude-opus-4-8", "p", deadline_s=120, purpose="billing-probe",
                    max_tokens=4096, attempts=3, backoff_s=0.01)
        check("all three attempts ran", calls["n"] == 3, str(calls["n"]))
        check("the Result carries the TOTAL billed, not the last attempt's",
              abs((r.cost or 0) - 0.60) < 1e-9, f"{r.cost} (expected 0.60 = 0.10+0.20+0.30)")
    finally:
        vc._attempt = orig


def test_a_single_attempt_still_reports_its_own_cost():
    def fake_attempt(*a, **k):
        return {"text": "hi", "out_tok": 5, "finish_reason": "end_turn", "cost": 0.42}

    orig = vc._attempt
    try:
        vc._attempt = fake_attempt
        r = vc.call("anthropic", "claude-opus-4-8", "p", deadline_s=60, purpose="billing-probe",
                    max_tokens=4096)
        check("no retries -> the one attempt's cost", abs((r.cost or 0) - 0.42) < 1e-9, str(r.cost))
    finally:
        vc._attempt = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_abandoned_calls_do_not_bill_invisibly: {failures} failure(s)")
    sys.exit(1 if failures else 0)
