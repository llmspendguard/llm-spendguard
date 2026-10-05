"""4b — the ROUTING MECHANISM (flagged, default OFF). Proves that SPENDGUARD_STORM_COALESCE routes a labelled
adapters.call through the shared storm coalescer (the implicit-fan door), that it is a no-op when OFF, and that a
concurrent labelled fan returns N->N with zero surfaced 429s. This test deliberately stays BELOW the realtime budget
so it exercises routing + pacing WITHOUT the batch path — the pace+batch COMBO under a *sustained* raw fan needs the
I20 cohort-reservation fix (see docs/PLAN_429_storm_to_batch.md), which is why 4b ships flagged OFF. Offline ($0):
realtime rides the governed path to the FakeProvider wall.
"""
import concurrent.futures as cf
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-4b-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = "2400"    # 40/s → a small fan clears fast, budget is large (all realtime)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import storm_harness as H  # noqa: E402
from spendguard import adapters, storm_route  # noqa: E402

MODEL = "anthropic:claude-haiku-4-5"
N = 12
fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _fan(n):
    """Fire n concurrent labelled adapters.call (the implicit-fan shape) and collect (result, surfaced_429)."""
    col = H.CallerCollector()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        out = list(ex.map(lambda i: adapters.call(MODEL, "do %s" % H.canary(i), intent="acc:4b", sig="acc:4b"), range(n)))
    for r in out:
        col.record(r)
    return out, col


# ── flag OFF (default): a labelled call runs DIRECT — no coalescer is created ─────────────────────────────────
fp = H.FakeProvider(cap=1000, window_s=1.0).install()
os.environ["SPENDGUARD_STORM_COALESCE"] = "0"
storm_route.reset_registry()
out_off, col_off = _fan(N)
ok_off = sum(1 for r in out_off if isinstance(r, dict) and r.get("text") and not r.get("error"))
verify_condition("flag OFF: N->N, zero surfaced 429, and NO coalescer created (direct path)",
                 ok_off == N and col_off.surfaced_429 == 0 and len(storm_route._REG) == 0,
                 extra="ok=%d/%d surfaced429=%d registry=%d" % (ok_off, N, col_off.surfaced_429, len(storm_route._REG)))

# ── flag ON: the SAME fan is ROUTED through the shared coalescer (cohorts>0), still N->N, zero surfaced 429 ────
os.environ["SPENDGUARD_STORM_COALESCE"] = "1"
storm_route.reset_registry()
out_on, col_on = _fan(N)
ok_on = sum(1 for r in out_on if isinstance(r, dict) and r.get("text") and not r.get("error"))
routed_cohorts = sum(getattr(c, "cohorts", 0) for c in storm_route._REG.values())
served_via = set(r.get("served_via") for r in out_on if isinstance(r, dict))
verify_condition("flag ON: routed through the coalescer (a coalescer exists and planned >=1 cohort)",
                 len(storm_route._REG) >= 1 and routed_cohorts >= 1,
                 extra="registry=%d cohorts=%d" % (len(storm_route._REG), routed_cohorts))
verify_condition("flag ON: N->N, zero surfaced 429, served via realtime (below budget → no batch needed)",
                 ok_on == N and col_on.surfaced_429 == 0 and served_via == {"realtime"},
                 extra="ok=%d/%d surfaced429=%d served_via=%s" % (ok_on, N, col_on.surfaced_429, served_via))
# demux: each returned result carries its own request's canary
mismatch = [i for i in range(N) if H._extract_canary(out_on[i].get("text")) != H.canary(i)]
verify_condition("flag ON: demux correct (result i carries canary i)", not mismatch, extra="mismatches=%s" % mismatch[:5])

storm_route.reset_registry()
fp.uninstall()
print("\n%s: test_storm_route_mechanism — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)
