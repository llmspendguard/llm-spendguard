"""A call that FAILED FAST measures the failure, not the work — and must not shrink the next budget.

WHY THIS GUARD EXISTS. note_latency recorded every outcome as a completed observation. A provider 400 comes
back in under a second, so 30 policy refusals on one call-class produced p50=0s and p99=10s, the derived
deadline collapsed to its 30s floor, and work that genuinely needs 15-30s per chunk then timed out — which
recorded another fast failure. A ratchet that tightens every time it fires: failures shrink the budget, the
smaller budget causes failures.

Exactly the shape of the truncation ratchet fixed earlier (truncated outputs measure the CAP, not the work,
so the more a class truncated the lower the recommendation went). Same rule, time dimension.

DEADLINE HITS ARE THE ONE EXCEPTION and are recorded, censored: they set a FLOOR, because the work was at
least that long. Everything else is counted in the call log for the reliability rate and contributes nothing
to the timing distribution.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-latcensor-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import inspect                                                    # noqa: E402
from spendguard import bulkgate, vendor_call as vc                # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


src = inspect.getsource(vc.call)
check("only OK and DEADLINE_EXCEEDED results feed the timing distribution",
      "last.ok or last.kind == DEADLINE_EXCEEDED" in src,
      "a fast failure recorded as a completed observation is what collapsed a 564s budget to 30s")

MODEL = "kimi-k3"
sig = bulkgate.sig(MODEL, template_id="ratchet-probe")
for _ in range(10):
    bulkgate.note_latency(sig, MODEL, 40.0)                       # real work: 40s a call
clean, _b = vc.time_budget("moonshot", MODEL, sig=sig, default_s=300)
check("a class of genuine 40s calls earns a budget well above the floor", clean > vc.DEADLINE_FLOOR_S,
      str(clean))

# The real case was not "a few failures among real calls" — it was a class whose observations were almost
# ENTIRELY fast failures, because every attempt was refused. p99 is robust to a minority of fast samples,
# which is exactly why the bug needed 30 refusals to bite and why it looked like a vendor problem.
sig_bad = bulkgate.sig(MODEL, template_id="all-refusals-probe")
for _ in range(30):
    bulkgate.note_latency(sig_bad, MODEL, 0.4)                    # what 30 policy 400s WOULD have recorded
poisoned, _b = vc.time_budget("moonshot", MODEL, sig=sig_bad, default_s=300)
check("a class of only fast failures yields a budget too small for real work",
      poisoned <= vc.DEADLINE_FLOOR_S, str(poisoned))
check("...far below the budget the same work earns from genuine observations", poisoned < clean,
      f"failures->{poisoned}s vs real work->{clean}s: the ratchet, in one line")

sig2 = bulkgate.sig(MODEL, template_id="floor-probe")
for _ in range(10):
    bulkgate.note_latency(sig2, MODEL, 30.0, hit_deadline=True)
d = bulkgate.latency(sig=sig2, model=MODEL)
check("a deadline hit is EXCLUDED from the percentiles", not d.get("n"), str(d.get("n")))
check("...but sets a floor, because the work was at least that long", d.get("floor") == 30.0, str(d.get("floor")))
check("...and is counted, so the hit rate stays visible", d.get("deadline_hits") == 10, str(d.get("deadline_hits")))

print(f"\n{'[FAIL]' if failures else 'OK'} test_only_completed_calls_measure_latency: {failures} failure(s)")
sys.exit(1 if failures else 0)
