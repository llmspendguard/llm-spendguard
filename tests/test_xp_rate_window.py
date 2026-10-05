"""Deterministic unit proof of the cross-process rate window's MATH (single process, fake clock, no real sleeps).
Each check uses a DISTINCT key (rows persist in the shared db within the process). The multi-PROCESS aggregate proof
is tests/test_xp_rate_window_multiprocess.py — only a real second process can prove cross-process conservation.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-xprate-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_DISPATCH_XP_OFF", None)
os.environ.pop("SPENDGUARD_DISPATCH_XP_RATE_OFF", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import xp_rate_window as X  # noqa: E402

fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


class _Clock:
    """A fake wall clock; sleep ADVANCES it, so reserve()'s wait-retry loop is deterministic and instant."""
    def __init__(self, t0=1000.0):
        self.t = t0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ── per-second sub-cap: 5 admit instantly, the 6th waits ~1s (the stampede bound) ─────────────────────────────
clk = _Clock()
lims = [(1.0, 5, None)]
waits = [X.reserve("k-persec", lims, clock=clk.now, sleep=clk.sleep, deadline_s=100) for _ in range(5)]
verify_condition("per-second: first 5 admit with ~0 wait", all(w < 0.001 for w in waits), extra="waits=%s" % waits)
w6 = X.reserve("k-persec", lims, clock=clk.now, sleep=clk.sleep, deadline_s=100)
verify_condition("per-second: the 6th waits ~1s (bounded burst, not a stampede)", 0.9 <= w6 <= 1.2, extra="wait=%.3f" % w6)

# ── 60s rpm window: 3 admit, the 4th waits ~60s ───────────────────────────────────────────────────────────────
clk = _Clock()
lims = [(60.0, 3, None)]
for _ in range(3):
    X.reserve("k-rpm", lims, clock=clk.now, sleep=clk.sleep, deadline_s=1000)
w4 = X.reserve("k-rpm", lims, clock=clk.now, sleep=clk.sleep, deadline_s=1000)
verify_condition("60s rpm: the 4th waits ~60s", 59.0 <= w4 <= 61.0, extra="wait=%.3f" % w4)

# ── tpm window: tokens, not count; an oversized single call is admitted (unavoidable), never wedged ───────────
clk = _Clock()
lims = [(60.0, None, 100)]
X.reserve("k-tpm", lims, est_tokens=40, clock=clk.now, sleep=clk.sleep, deadline_s=1000)    # 40
X.reserve("k-tpm", lims, est_tokens=40, clock=clk.now, sleep=clk.sleep, deadline_s=1000)    # 80
w3 = X.reserve("k-tpm", lims, est_tokens=40, clock=clk.now, sleep=clk.sleep, deadline_s=1000)  # 120>100 → wait
verify_condition("tpm: a call that would exceed the token window waits", w3 >= 59.0, extra="wait=%.3f" % w3)
clk2 = _Clock()
w_big = X.reserve("k-tpm-big", lims, est_tokens=500, clock=clk2.now, sleep=clk2.sleep, deadline_s=5)  # est>max, empty → admit alone
verify_condition("tpm: a lone oversized call (empty window) is ADMITTED (unavoidable), not wedged", w_big < 0.001,
                 extra="wait=%.3f (should admit immediately)" % w_big)
# the oversized call is RECORDED (not a free bypass): the NEXT request on that key must WAIT for it to drain
w_after_big = X.reserve("k-tpm-big", lims, est_tokens=10, clock=clk2.now, sleep=clk2.sleep, deadline_s=1000)
verify_condition("tpm: after a lone-oversized admit, the next request WAITS (oversized was recorded, no free bypass)",
                 w_after_big >= 59.0, extra="wait=%.3f (the 500-tok row must hold the window)" % w_after_big)
# a SECOND oversized request does not also bypass while the first is in-window
clk3 = _Clock()
X.reserve("k-tpm-big2", lims, est_tokens=500, clock=clk3.now, sleep=clk3.sleep, deadline_s=5)   # 1st admits alone
w_big2 = X.reserve("k-tpm-big2", lims, est_tokens=500, clock=clk3.now, sleep=clk3.sleep, deadline_s=1000)  # 2nd must wait
verify_condition("tpm: two oversized requests do NOT both bypass — the 2nd waits for the 1st to drain",
                 w_big2 >= 59.0, extra="wait=%.3f (second oversized must not stampede)" % w_big2)

# ── multi-window: a per-second sub-cap blocks even when the minute has ample room ─────────────────────────────
clk = _Clock()
lims = X.limits_for(rpm=600, per_second_burst=1.0)   # 600/min AND ceil(600/60*1)=10/s
for _ in range(10):
    X.reserve("k-multi", lims, clock=clk.now, sleep=clk.sleep, deadline_s=1000)   # 10 in the first second — minute has 590 to spare
w11 = X.reserve("k-multi", lims, clock=clk.now, sleep=clk.sleep, deadline_s=1000)
verify_condition("multi-window: 11th blocks on the 1s sub-cap though the 60s minute has room", 0.9 <= w11 <= 1.2,
                 extra="wait=%.3f (per-second cap 10 bound it, not the minute)" % w11)

# ── kill switch ───────────────────────────────────────────────────────────────────────────────────────────────
os.environ["SPENDGUARD_DISPATCH_XP_RATE_OFF"] = "1"
clk = _Clock()
offw = [X.reserve("k-off", [(1.0, 1, None)], clock=clk.now, sleep=clk.sleep, deadline_s=100) for _ in range(5)]
os.environ.pop("SPENDGUARD_DISPATCH_XP_RATE_OFF", None)
verify_condition("kill switch: XP_RATE_OFF=1 admits everything (0 wait)", all(w == 0.0 for w in offw), extra="waits=%s" % offw)

print("\n%s: test_xp_rate_window — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)
