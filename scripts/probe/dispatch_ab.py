"""A/B/C the lane-fan DISPATCH — STATIC round-robin vs DYNAMIC least-loaded vs DYNAMIC + TAIL-HEDGE — on a real
batch across the good lanes.

Reports, per policy, wall-clock + the lane SPREAD (tasks served per lane), so BOTH the speedup and the cross-vendor
diversity are visible at once (the concern: least-loaded must not collapse the panel to one fast lane, and hedging
must not silently skew it toward the fastest). $0 — plan-served lanes only (refuse_billed=True: a lane miss is a
free error row, never a metered call). The hedge itself is always $0 by construction. Facts only, no verdict — the
reader compares. Alternates the policies to blunt warm/cold order bias.

The three policies:
  · STATIC round-robin  — arms[i % n]; the old head-of-line-blocking assignment (SPENDGUARD_LANE_STATIC_DISPATCH=1).
  · DYNAMIC least-loaded — pick the most-free arm per task (the committed default); balances LANES.
  · DYNAMIC + hedge      — same, plus fire a duplicate on the most-free OTHER lane when a task stalls past HEDGE_MS,
                           taking whichever returns first; rescues a single slow CALL. `hedged`/`hedge_peer` on a row
                           mark a raced task, so the spread line also prints how many tasks hedged (skew visibility).

Run under the gate:  .venv.nosync/bin/python scripts/probe/dispatch_ab.py
  env: N=<batch> ROUNDS=<n> HEDGE_MS=<ms>   (defaults: N=16 ROUNDS=2 HEDGE_MS=2000)
"""
import os
import time
import collections

import spendguard  # noqa: F401  (gated package)
from spendguard import lane_balance

INTENT = "probe:dispatch-ab"
N = int(os.environ.get("N", "16"))
ROUNDS = int(os.environ.get("ROUNDS", "2"))
HEDGE_MS = os.environ.get("HEDGE_MS", "2000")
# lanes= pins the fan to these arms and DISABLES bandit substitution (no_substitution) — so the row's `lane` is the
# DISPATCH's pick, not a bandit swap. This is honestreview's effective world (its review intents are bandit-
# denylisted), and the only way to measure the dispatch policy itself rather than the bandit.
LANES = ["codex", "zai-coding", "gemini", "claude-code"]

# (label, static_dispatch, hedge_on)
POLICIES = [
    ("STATIC round-robin",  True,  False),
    ("DYNAMIC least-loaded", False, False),
    ("DYNAMIC + hedge",      False, True),
]


def _run(static, hedge_on):
    if static:
        os.environ["SPENDGUARD_LANE_STATIC_DISPATCH"] = "1"
    else:
        os.environ.pop("SPENDGUARD_LANE_STATIC_DISPATCH", None)
    if hedge_on:
        os.environ["SPENDGUARD_DISPATCH_LANE_HEDGE_MS"] = str(HEDGE_MS)
    else:
        os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)
    tasks = [f"Reply with exactly one word: ok. (item {i})" for i in range(N)]   # distinct → no content dedup
    t0 = time.time()
    rows = lane_balance.bulk_delegate(tasks, intent=INTENT, force=True, deadline_s=90, refuse_billed=True, lanes=LANES)
    wall = time.time() - t0
    served = [r for r in rows if r.get("text")]
    spread = collections.Counter(r.get("lane") for r in served)
    hedged = sum(1 for r in served if r.get("hedged"))
    return wall, len(served), dict(spread), hedged


print(f"[dispatch A/B/C] intent={INTENT}  N={N}  rounds={ROUNDS}  hedge_ms={HEDGE_MS}  (lower wall = better; spread "
      f"across lanes = diversity preserved; hedged = tasks that raced a 2nd lane)\n")
agg = {label: [] for label, _s, _h in POLICIES}
for r in range(ROUNDS):
    for label, static, hedge_on in POLICIES:
        wall, ok, spread, hedged = _run(static, hedge_on)
        agg[label].append(wall)
        _htxt = f"  hedged {hedged}" if hedge_on else ""
        print(f"  round {r+1}  {label:22} wall {wall:6.1f}s  ok {ok}/{N}  spread {spread}{_htxt}")
print()
for label in agg:
    walls = agg[label]
    if walls:
        print(f"  {label:22} median wall {sorted(walls)[len(walls)//2]:6.1f}s  (runs: {[round(w,1) for w in walls]})")
