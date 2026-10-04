"""MULTI-PROCESS proof (panel F1 / the single most important missing invariant): P separate processes fan requests
at ONE shared rate db; the AGGREGATE egress over any sliding 1s window must stay <= the per-second cap, and every
request must eventually admit (conservation). An in-process test cannot prove this — the whole point is that two
coalescer/governor instances in different processes must share ONE budget. This is the un-fakeable cross-process check.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix="sg-xprate-mp-")
P = 3                      # processes
M = 25                     # reservations each
CAP = 25                   # per-second aggregate cap across ALL processes
KEY = "vendor:acceptance-mp"

env = dict(os.environ)
env["SPENDGUARD_HOME"] = HOME                            # all workers share ONE rate db
env["SPENDGUARD_TEST_ISOLATED"] = "1"
env["SPENDGUARD_NO_AUTOINSTALL"] = "1"
env.pop("SPENDGUARD_DISPATCH_XP_OFF", None)
env.pop("SPENDGUARD_DISPATCH_XP_RATE_OFF", None)

outs = [os.path.join(HOME, "stamps_%d.json" % i) for i in range(P)]
t0 = time.monotonic()
procs = [subprocess.Popen([sys.executable, os.path.join(_HERE, "_xp_rate_worker.py"), KEY, str(M), str(CAP), outs[i]],
                          env=env) for i in range(P)]
rc = [p.wait() for p in procs]
elapsed = time.monotonic() - t0

fails = []
if any(r != 0 for r in rc):
    fails.append("a worker exited non-zero: %s" % rc)

stamps = []
for o in outs:
    try:
        with open(o) as fh:
            stamps.extend(json.load(fh))
    except Exception as e:
        fails.append("missing/unreadable worker output %s: %r" % (os.path.basename(o), e))

total = len(stamps)
# GLOBAL sliding-1s peak across every process's admissions
ss = sorted(stamps)
peak = max((sum(1 for b in ss if a <= b < a + 1.0) for a in ss), default=0)
# pacing sanity: P*M admissions at CAP/s cannot finish faster than ~(P*M/CAP) seconds
min_expected_s = (P * M) / float(CAP) * 0.6

if total != P * M:
    fails.append("conservation: %d/%d admitted (some lost)" % (total, P * M))
if peak > CAP + 2:                                       # +2 boundary slack for the few-ms stamp-vs-admit skew
    fails.append("AGGREGATE per-second peak %d exceeds cap %d (+2) — cross-process budget NOT shared" % (peak, CAP))
if elapsed < min_expected_s:
    fails.append("finished in %.2fs < ~%.2fs floor — not actually paced across processes" % (elapsed, min_expected_s))

if fails:
    print("RED: test_xp_rate_window_multiprocess")
    for f in fails:
        print("   RED:", f)
    sys.exit(1)
print("ALL GREEN: test_xp_rate_window_multiprocess — %d procs x %d = %d admitted, aggregate 1s-peak %d <= cap %d, %.2fs"
      % (P, M, total, peak, CAP, elapsed))
sys.exit(0)
