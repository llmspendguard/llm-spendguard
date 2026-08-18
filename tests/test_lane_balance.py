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

from spendguard import lane_balance, receipt                                           # noqa: E402


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
    "gemini":      {"by_day": {today: 40.0},   "asof": today},
    # "zai-coding" intentionally ABSENT — a never-used lane must read as idle, not crash
}}
p = receipt._cache_path()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cache))

_orig_fee = receipt._plan_usd
try:
    receipt._plan_usd = lambda: (400.0, False)        # $400 total / 4 lanes → $100 each (the approx split)
    u = lane_balance.lane_utilization()
    by = {l["lane"]: l for l in u["lanes"]}

    print("-- per-plan classification (hot >= 1.5x fee, idle < 0.5x fee; defaults) --")
    fails += ck("claude-code HOT (900/100 = 9x)", by["claude-code"]["state"] == "hot")
    fails += ck("codex IDLE (5/100 = 0.05x)", by["codex"]["state"] == "idle")
    fails += ck("gemini IDLE (40/100 = 0.4x < 0.5)", by["gemini"]["state"] == "idle")
    fails += ck("a lane with NO source record reads as idle (0 est-value), not a crash",
                by["zai-coding"]["state"] == "idle" and by["zai-coding"]["est_value_month"] == 0.0)
    fails += ck("utilization = est-value / fee", abs(by["claude-code"]["utilization"] - 9.0) < 0.001)
    fails += ck("per-lane fee is the even split, flagged NOT exact (no lane_plans map set)",
                by["claude-code"]["plan_fee"] == 100.0 and by["claude-code"]["fee_exact"] is False)

    print("\n-- the router's inputs: shed-from and absorb-into, least-utilised first --")
    fails += ck("hot_lanes() = [claude-code]", lane_balance.hot_lanes() == ["claude-code"])
    idle = lane_balance.idle_lanes()
    fails += ck("idle_lanes() lists the three unused plans", set(idle) == {"codex", "gemini", "zai-coding"})
    fails += ck("...least-utilised FIRST (zai/codex before gemini)", idle.index("gemini") == len(idle) - 1)

    print("\n-- honesty: it is est-VALUE, and it says so (never billed, never provider quota) --")
    txt = lane_balance.format_utilization()
    fails += ck("readout labels the axis est-value / NOT billed / NOT quota",
                "est-value" in txt and "NOT billed" in txt and "quota" in txt)
finally:
    receipt._plan_usd = _orig_fee

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_balance: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
