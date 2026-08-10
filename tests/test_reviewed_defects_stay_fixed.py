"""Defects found by a 4-vendor review and confirmed by two independent validators, each with a guard.

WHY THIS FILE EXISTS. A fix without a test is a fix that comes back — that is how `_MARKER_MODELS` sat
defined-and-unreferenced and how output_cap's measured rung could never fire. Every entry below was:
found by 2+ vendors independently, then confirmed by BOTH opus and gpt-5.5 reading the real source. The
gate was itself checked with a negative control: a fabricated defect against code that explicitly guards
the case was rejected by both validators, each citing the guard by line.

The pattern in the first two is worth naming: BOTH are the project's core invariant violated inside the
code written to enforce it. That is not irony, it is the reason external review is worth paying for — the
author is the last person able to see it.
"""
import contextlib
import io
import sys

from spendguard import expected_output as eo, reconcile

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_expected_output_never_returns_a_SILENT_zero():
    """expected_output.py:79 — 2 vendors, both validators, HIGH.

    warn_unknown() existed, said "an estimate that silently treats unknown output as ZERO is the bug this
    module exists to end", and expect() returned the zero WITHOUT EVER CALLING IT. The module that forbids
    silent zeros was producing one."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        n, basis = eo.expect("a-model-that-cannot-possibly-be-priced-xyz")
    check("the unknown path still returns a number a caller can do arithmetic with", n == 0)
    check("...and NAMES itself unknown", basis == "unknown", basis)
    check("...and is NOT silent about it", bool(err.getvalue().strip()),
          "returning 0 quietly is the exact failure this module was written to prevent")


def test_a_measured_provider_total_of_zero_is_not_treated_as_absence():
    """reconcile.py:63 — 2 vendors, both validators.

    `if not truth_total: return None` swallowed 0.0 four lines below a docstring promising that a failed
    fetch never reads as "$0 / 100% covered". A provider that billed NOTHING while we attribute spend to it
    is the loudest leak there is, and it was the quietest."""
    msg = reconcile.residual_warning(0.0, 12.50)
    check("truth=$0 with $12.50 attributed WARNS", bool(msg), repr(msg))
    check("...and says the number is not reconciled", msg and "reconcile" in msg.lower(), str(msg)[:80])
    check("truth=$0 with $0 attributed stays quiet — nothing is wrong there",
          reconcile.residual_warning(0.0, 0.0) is None)
    check("truth=None (fetch FAILED) is still distinct from truth=0 (provider billed nothing)",
          "UNKNOWN" in (reconcile.residual_warning(None, 12.50) or ""),
          "an unreadable bill and a zero bill are different facts and must not share a message")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    

# ══════════════════════════════════════════════════════════════════════════════════════════════════
# WAVE 1, the singletons. Each was confirmed by both validators reading the real source.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
from spendguard import cascade, callio, close, output_contract          # noqa: E402

print("\n-- nothing tried is not something tried (cascade) --")
# An empty ladder skipped the loop and returned n_tried=1 with output='' — shaped exactly like a model
# answering with an empty string, which is a completely different event.
_r = cascade.cascade("hi", [], intent=None, _caller=lambda m, p: (0.0, "x"))
check("an empty ladder reports n_tried=0, not 1", _r["n_tried"] == 0)
check("...and output is None, not '' (which reads as an empty answer)", _r["output"] is None)
check("...and it says WHY rather than returning a bare shape", bool(_r.get("why")))

# A rung that RAISES is a rung that failed, and escalating past a failure is what a cascade is for.
_calls = []
def _flaky(m, p):
    _calls.append(m)
    if m == "cheap":
        raise TimeoutError("cheap model timed out")
    return (0.01, "answer from " + m)
_r = cascade.cascade("hi", ["cheap", "strong"], verify=lambda p, o: True, _caller=_flaky)
check("a rung that raises escalates instead of killing the run", _r["model"] == "strong", str(_r))
check("...and the failure is RECORDED, not swallowed", bool(_r.get("errors")), str(_r.get("errors")))
check("...and both rungs were actually attempted", _calls == ["cheap", "strong"], str(_calls))

print("\n-- limit=0 means no rows, not no limit (callio) --")
# unjudged(0) returned EVERY unjudged row. The caller most likely to pass 0 is a budget loop that has run
# out of room — the worst possible moment to hand back the whole table.
check("unjudged(0) returns nothing", callio.unjudged(0) == [])

print("\n-- a callable's identity includes its module (output_contract) --")
# Two modules each defining `validate` produced the same identity, so one contract's test flag satisfied
# the other's — the exact staleness this identity exists to expire.
def validate(x):        # noqa: E306
    return True
_a = output_contract.describe(validate)
validate.__module__ = "some.other.module"
_b = output_contract.describe(validate)
check("same qualname in different modules -> different identities", _a != _b, f"{_a} vs {_b}")

print("\n-- a month string is parsed, not eyeballed (close) --")
# `len==7 and [4]=="-"` accepted '2024-99', which built a window no row can fall in and closed the month
# at $0.00 without complaint.
for _bad in ("2024-ab", "2024-99", "2024-00"):
    _out = io.StringIO()
    with contextlib.redirect_stdout(_out):
        _rc = close.main(["--month", _bad])
    check(f"--month {_bad} is refused with a usage message", _rc == 2 and "YYYY-MM" in _out.getvalue())

print(f"\n{'[FAIL]' if failures else 'OK'} test_reviewed_defects_stay_fixed: {failures} failure(s)")
sys.exit(1 if failures else 0)
