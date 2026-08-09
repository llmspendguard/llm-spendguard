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
    print(f"\n{'[FAIL]' if failures else 'OK'} test_reviewed_defects_stay_fixed: {failures} failure(s)")
    sys.exit(1 if failures else 0)
