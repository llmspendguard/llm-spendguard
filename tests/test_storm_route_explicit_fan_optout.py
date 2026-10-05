"""4b REGRESSION LOCK — the explicit-fan opt-out, under DEFAULT-ON.

4b routing is ON by default, but it must route only the RAW implicit fan. The explicit fans (bulk_delegate/submit_storm)
pass `_coalesce_eligible=False` because they are ALREADY governed by the dispatch connection-window and carry their own
Batch-API offload — re-coalescing them replaces a proven ~1s realtime path with a batch divert (minutes) and was what
turned test_connection_storm_reliability RED when 4b was naively flipped on. This test pins the seam: with the flag
UNSET (proving the default is ON), an eligible raw fan is routed through a coalescer, while an identical fan carrying
`_coalesce_eligible=False` is NOT — no coalescer is created and each call runs its normal direct governed path.

Offline ($0): the realtime wall is tests/storm_harness.FakeProvider; nothing hits a network or a real Batch API.
"""
import concurrent.futures as cf
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-4b-optout-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = "2400"    # 40/s → a small fan clears in realtime (no batch needed)
os.environ.pop("SPENDGUARD_STORM_COALESCE", None)           # DO NOT set it — this test proves the DEFAULT is ON
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard import adapters, storm_route  # noqa: E402

MODEL = "anthropic:claude-haiku-4-5"
N = 8
fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _fan(eligible):
    """Fire N concurrent labelled metered adapters.call; `eligible=False` sets the explicit-fan opt-out."""
    kw = {} if eligible else {"_coalesce_eligible": False}
    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        return list(ex.map(lambda i: adapters.call(MODEL, "do %s" % H.canary(i), intent="acc:optout",
                                                   sig="acc:optout", metered_only=True, **kw), range(N)))


fp = H.FakeProvider(cap=100000, window_s=1.0).install()
try:
    # confirm the precondition THIS test rests on: routing really is ON by default (flag unset)
    verify_condition("precondition: 4b is ON by default (flag unset)", storm_route.coalescing_enabled())

    # ── ELIGIBLE (a raw fan, default _coalesce_eligible=True): ROUTED through a coalescer ──────────────────────────
    storm_route.reset_registry()
    out_elig = _fan(eligible=True)
    ok_elig = sum(1 for r in out_elig if isinstance(r, dict) and r.get("text") and not r.get("error"))
    routed_via = set(r.get("served_via") for r in out_elig if isinstance(r, dict))
    verify_condition("eligible raw fan: N->N and a coalescer was created (routed)",
                     ok_elig == N and len(storm_route._REG) >= 1,
                     extra="ok=%d/%d registry=%d" % (ok_elig, N, len(storm_route._REG)))
    verify_condition("eligible raw fan: served via the coalescer (served_via=realtime)",
                     routed_via == {"realtime"}, extra="served_via=%s" % routed_via)

    # ── OPTED OUT (_coalesce_eligible=False, the bulk_delegate/submit_storm signal): NOT routed ───────────────────
    storm_route.reset_registry()
    before_rt = fp.realtime_calls
    out_opt = _fan(eligible=False)
    ok_opt = sum(1 for r in out_opt if isinstance(r, dict) and r.get("text") and not r.get("error"))
    served_direct = all(r.get("served_via") is None for r in out_opt if isinstance(r, dict))
    verify_condition("opted-out fan: N->N but NO coalescer created (direct governed path)",
                     ok_opt == N and len(storm_route._REG) == 0,
                     extra="ok=%d/%d registry=%d" % (ok_opt, N, len(storm_route._REG)))
    verify_condition("opted-out fan: every call bypassed the coalescer (no served_via tag) and hit the wall directly",
                     served_direct and fp.realtime_calls > before_rt,
                     extra="served_direct=%s realtime_delta=%d" % (served_direct, fp.realtime_calls - before_rt))
finally:
    storm_route.reset_registry()
    fp.uninstall()

print("\n%s: test_storm_route_explicit_fan_optout — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)
