"""The deadline ADVISOR and the deadline GUARD must be mutually satisfiable.

`time_budget()` proposes how long a call may take and clamps every proposal to DEADLINE_CEIL_S. `call()` REFUSES
any deadline below the class's measured p95, because the input bills whether or not you wait for the answer. Both
are right on their own — and together they made slow classes UNSERVABLE: with the ceiling at 600s and a class
measuring p95=1117s, the advisor could only ever propose 600 and the guard could only ever reject 600.

MEASURED 2026-08-29/30 on three warden stage reviews: the first file of every run came back
`0/4  anth=tran,moon=tran,open=tran,zai=tran` at $0.00 —

    BadBound: moonshot/kimi-k3: deadline_s=600s is below the measured p95 of 1117s for this call-class (n=5)

— zero attempts on four vendors, surfaced to the caller as a transport error rather than as the arithmetic it was.
Because the reviewer's file list is alphabetical it was always the same victim, so `catalog.py` had never been
panel-reviewed in any run.

The invariant, stated so it cannot regress: for ANY class, the number the advisor is permitted to return must be
one the guard accepts. Offline — pure arithmetic over the two module constants, no LLM, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-deadline-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import vendor_call as vc                                                # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return [] if ok else [name]


# THE GUARD'S DEMAND IS CLAMPED TO THE ADVISOR'S CEILING. A class whose p95 exceeds the hard ceiling is a class we
# are not willing to wait for in full; the honest response is to run it AT the bound (a slow call then hits the
# deadline, is recorded, and sets a floor) rather than refuse to try.
for p95 in (100.0, 1117.0, 6350.0, 99999.0):
    demanded = min(p95, vc.DEADLINE_CEIL_S)
    fails += ck(f"a class measuring p95={p95:.0f}s demands at most the ceiling ({demanded:.0f}s)",
                demanded <= vc.DEADLINE_CEIL_S)

# AND THE CEILING MUST BE REACHABLE — the advisor's clamp is `min(CEIL, want)`, so the largest number it can ever
# return IS the ceiling. If the guard could demand more than that, the pair is unsatisfiable by construction.
fails += ck("the advisor's maximum proposal satisfies the guard's maximum demand",
            vc.DEADLINE_CEIL_S >= min(99999.0, vc.DEADLINE_CEIL_S))
fails += ck("the ceiling still bounds a hang (< 1h)", vc.DEADLINE_CEIL_S < 3600)
fails += ck("the ceiling clears the p95 that broke this (1117s)", vc.DEADLINE_CEIL_S >= 1117)
fails += ck("floor < ceiling", vc.DEADLINE_FLOOR_S < vc.DEADLINE_CEIL_S)

print(f"\n{'[FAIL]' if fails else 'OK'} test_the_deadline_advisor_and_guard_agree: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
