"""Proactive lane-utilisation brain — per-plan est-value ÷ fee decides HOT (shed from) vs IDLE (absorb overflow).
The numbers reuse the receipt's OWN per-source cache + re-windowing, so they match the receipt; these guards pin the
classification, the fee split, the absent-source=idle case, and the least-utilised-first ordering the router relies
on. Offline: a stubbed est-value cache, no receipt recompute, no LLM.
"""
import os
import sys
import json
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanebal-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, receipt, lane_catalog                             # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

# A deterministic est-value cache: claude-code saturated, codex barely used, gemini idle, zai-coding ABSENT (=0).
# by_day is dated TODAY so _rewindow buckets it into this month regardless of when the test runs.
today = receipt._windows()[0]
cache = {"est_value_by_source": {
    "claude-code": {"by_day": {today: 900.0}, "asof": today},
    "codex":       {"by_day": {today: 5.0},   "asof": today},
    "gemini":      {"by_day": {today: 20.0},   "asof": today},
    # "zai-coding" (and "kimi-code") intentionally ABSENT — a never-used lane must read as idle, not crash
}}
p = receipt._cache_path()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cache))

_orig_fee = receipt._plan_usd
_fee = 400.0 / len(lane_catalog.lanes())          # DERIVE the per-lane split from the REAL lane count — adding a lane
#                                                   re-splits the flat fee, so the test must not assume a lane count.
try:
    receipt._plan_usd = lambda: (400.0, False)        # $400 total / N lanes → the even split
    u = lane_balance.lane_utilization()
    by = {l["lane"]: l for l in u["lanes"]}

    print("-- per-plan classification (hot >= 1.5x fee, idle < 0.5x fee; defaults) --")
    fails += ck("claude-code HOT (900/fee well over 1.5x)", by["claude-code"]["state"] == "hot")
    fails += ck("codex IDLE (5/fee well under 0.5x)", by["codex"]["state"] == "idle")
    fails += ck("gemini IDLE (20/fee under 0.5x)", by["gemini"]["state"] == "idle")
    fails += ck("a lane with NO source record reads as idle (0 est-value), not a crash",
                by["zai-coding"]["state"] == "idle" and by["zai-coding"]["est_value_month"] == 0.0)
    fails += ck("utilization = est-value / fee", abs(by["claude-code"]["utilization"] - 900.0 / _fee) < 0.001)
    fails += ck("per-lane fee is the even split, flagged NOT exact (no lane_plans map set)",
                abs(by["claude-code"]["plan_fee"] - _fee) < 0.001 and by["claude-code"]["fee_exact"] is False)

    print("\n-- the router's inputs: shed-from and absorb-into, least-utilised first --")
    fails += ck("hot_lanes() = [claude-code]", lane_balance.hot_lanes() == ["claude-code"])
    idle = lane_balance.idle_lanes()
    fails += ck("idle_lanes() lists every lane except the hot one (derived, not a hardcoded set)",
                set(idle) == set(lane_catalog.lanes()) - {"claude-code"})
    fails += ck("...least-utilised FIRST (the highest-value idle lane, gemini, is last)",
                idle.index("gemini") == len(idle) - 1)

    print("\n-- honesty: it is est-VALUE, and it says so (never billed, never provider quota) --")
    txt = lane_balance.format_utilization()
    fails += ck("readout labels the axis est-value / NOT billed / NOT quota",
                "est-value" in txt and "NOT billed" in txt and "quota" in txt)
finally:
    receipt._plan_usd = _orig_fee

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_balance: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
