"""A/B the lane-fan DISPATCH — STATIC round-robin vs DYNAMIC least-loaded — on a real batch across the good lanes.

Reports, per policy, wall-clock + the lane SPREAD (tasks served per lane), so BOTH the speedup and the cross-vendor
diversity are visible at once (the concern: least-loaded must not collapse the panel to one fast lane). $0 —
plan-served lanes only (refuse_billed=True: a lane miss is a free error row, never a metered call). Facts only, no
verdict — the reader compares. Alternates the two policies to blunt warm/cold order bias.

Run under the gate:  .venv.nosync/bin/python scripts/probe/dispatch_ab.py   (env N=<batch>, ROUNDS=<n>)
"""
import os
import time
import collections

import spendguard  # noqa: F401  (gated package)
from spendguard import lane_balance

INTENT = "probe:dispatch-ab"
N = int(os.environ.get("N", "16"))
ROUNDS = int(os.environ.get("ROUNDS", "2"))


def _run(static):
    if static:
        os.environ["SPENDGUARD_LANE_STATIC_DISPATCH"] = "1"
    else:
        os.environ.pop("SPENDGUARD_LANE_STATIC_DISPATCH", None)
    tasks = [f"Reply with exactly one word: ok. (item {i})" for i in range(N)]   # distinct → no content dedup
    # lanes= pins the fan to these arms and DISABLES bandit substitution (no_substitution) — so the row's `lane` is
    # the DISPATCH's pick, not a bandit swap. This is honestreview's effective world (its review intents are
    # bandit-denylisted), and the only way to measure the dispatch policy itself rather than the bandit.
    LANES = ["codex", "zai-coding", "gemini", "claude-code"]
    t0 = time.time()
    rows = lane_balance.bulk_delegate(tasks, intent=INTENT, force=True, deadline_s=90, refuse_billed=True, lanes=LANES)
    wall = time.time() - t0
    served = [r for r in rows if r.get("text")]
    spread = collections.Counter(r.get("lane") for r in served)
    return wall, len(served), dict(spread)


print(f"[dispatch A/B] intent={INTENT}  N={N}  rounds={ROUNDS}  (wall-clock + per-lane spread; lower wall = better, "
      f"spread across lanes = diversity preserved)\n")
agg = {"STATIC round-robin": [], "DYNAMIC least-loaded": []}
for r in range(ROUNDS):
    for label, static in (("STATIC round-robin", True), ("DYNAMIC least-loaded", False)):
        wall, ok, spread = _run(static)
        agg[label].append(wall)
        print(f"  round {r+1}  {label:22} wall {wall:6.1f}s  ok {ok}/{N}  spread {spread}")
print()
for label, walls in agg.items():
    if walls:
        print(f"  {label:22} median wall {sorted(walls)[len(walls)//2]:6.1f}s  (runs: {[round(w,1) for w in walls]})")
