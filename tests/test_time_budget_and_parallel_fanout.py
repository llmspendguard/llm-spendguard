"""A deadline is a termination bound for TIME — measured, per vendor — and a panel must run CONCURRENTLY.

WHY THIS GUARD EXISTS. Two defects made a four-LLM panel neither reliable nor timely:

  1. `fan_out` was a list comprehension, so N vendors ran one after another and the panel cost the SUM of
     their latencies. Measured p90s were openai 20.6s, anthropic 24.0s, moonshot 116.8s, zai 180.0s: over
     five minutes of wall-clock to do twenty seconds of parallel work, with the slowest vendor setting the
     pace for every question.
  2. The deadline was a hardcoded global 180s across all four, while their real p90s differ by ~6x. That is
     the same defect as a guessed max_tokens, and it fails the same way: too low and the call dies AFTER the
     input is paid for (deadline_exceeded is 100% waste, exactly like truncation); too high and you wait
     three minutes to learn a vendor is down.

`first_ok` exists for the third case: when the job needs an answer rather than agreement, waiting on the
straggler buys nothing.
"""
import os, sys, tempfile, time

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-timebudget-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import inspect                                                    # noqa: E402
from spendguard import bulkgate, vendor_call as vc                # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


# ── the budget is MEASURED, not guessed ──────────────────────────────────────────────────────────────
b, basis = vc.time_budget("openai", "no-history-model-xyz")
check("no measurement and no caller default -> UNKNOWN, never an invented number", (b, basis) == (None, "unknown"))

b, basis = vc.time_budget("openai", "no-history-model-xyz", default_s=45)
check("the caller's own number is used when nothing is measured", (b, basis) == (45.0, "caller"))

sig = bulkgate.sig("fastmodel", template_id=None)
for _ in range(10):
    bulkgate.note_latency(sig, "fastmodel", 4.0)
b, basis = vc.time_budget("openai", "fastmodel", default_s=180)
check("a MEASURED budget overrides the caller's global guess", basis.startswith("measured"), basis)
check("...and it is far tighter than the 180s that was hardcoded for every vendor", b < 180, str(b))
check("...but never below the floor, so a healthy call cannot be killed by a tight measurement",
      b >= vc.DEADLINE_FLOOR_S, str(b))

slow = bulkgate.sig("slowmodel", template_id=None)
for _ in range(10):
    bulkgate.note_latency(slow, "slowmodel", 100.0)
b_slow, _ = vc.time_budget("zai", "slowmodel", default_s=180)
check("a slow vendor gets a LONGER budget than a fast one — one number cannot fit both", b_slow > b,
      f"{b_slow} vs {b}")

# ── a call that HIT the deadline measures the deadline, not the work ─────────────────────────────────
cens = bulkgate.sig("censormodel", template_id=None)
for _ in range(10):
    bulkgate.note_latency(cens, "censormodel", 20.0)
before, _ = vc.time_budget("zai", "censormodel", default_s=180)
for _ in range(10):
    bulkgate.note_latency(cens, "censormodel", 30.0, hit_deadline=True)
after, _ = vc.time_budget("zai", "censormodel", default_s=180)
check("timed-out calls never DRAG the budget down (the ratchet that kills more the more it kills)",
      after >= before, f"{before} -> {after}")

d = bulkgate.latency(cens)
check("timed-out calls are excluded from the percentiles but still COUNTED", d["n"] == 10 and d["deadline_hits"] == 10)
check("...and the hit rate is reported, so 'reliable' is a number you can see", abs(d["hit_rate"] - 0.5) < 1e-9)

# ── the panel runs CONCURRENTLY ──────────────────────────────────────────────────────────────────────
src = inspect.getsource(vc.fan_out)
check("fan_out no longer calls vendors in a sequential comprehension",
      "ThreadPoolExecutor" in src and "for v, m in vendors]" not in src)
check("fan_out gives each vendor its OWN measured budget", "time_budget(" in src)

_orig = vc.call
try:
    def _slow_call(vendor, model, prompt, **kw):
        time.sleep(0.4)
        return vc.Result(vc.OK, vendor, model, text="hi", out_tok=3)
    vc.call = _slow_call
    t0 = time.time()
    fan = vc.fan_out([("a", "m1"), ("b", "m2"), ("c", "m3"), ("d", "m4")], "q", deadline_s=30)
    el = time.time() - t0
    check("four 0.4s vendors complete in well under the 1.6s a sequential panel would take", el < 1.0, f"{el:.2f}s")
    check("...and every vendor's result is present", fan["n_ok"] == 4 and fan["complete"])

    def _mixed_call(vendor, model, prompt, **kw):
        time.sleep(0.1 if vendor == "fast" else 3.0)
        return vc.Result(vc.OK, vendor, model, text="hi", out_tok=3)
    vc.call = _mixed_call
    t0 = time.time()
    r = vc.first_ok([("fast", "m1"), ("slow", "m2")], "q", deadline_s=30, need=1)
    el = time.time() - t0
    check("first_ok returns at the FAST vendor's latency, not the straggler's", el < 1.5, f"{el:.2f}s")
    check("...and says so honestly: complete means `need` was MET", r["complete"] and r["n_ok"] == 1)
finally:
    vc.call = _orig

print(f"\n{'[FAIL]' if failures else 'OK'} test_time_budget_and_parallel_fanout: {failures} failure(s)")
sys.exit(1 if failures else 0)
