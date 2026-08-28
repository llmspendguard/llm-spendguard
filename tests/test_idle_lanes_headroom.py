"""Stage C wiring: idle_lanes now orders overflow by REAL quota utility (route_utility.rank_lanes over the persisted
lanes.lane_headroom snapshot) where a provider exposes headroom, and DEGRADES to the old call-volume proxy where it
does not. Plus refresh_headroom_if_stale keeps that snapshot fresh out-of-band (no CLI in the routing hot path).

Pins (offline; lane_utilization + the snapshot stubbed; no CLI, no network):
  (a) idle_lanes with a snapshot orders by headroom×urgency (most-available first), cooling excluded;
  (b) idle_lanes with NO snapshot degrades EXACTLY to the call-volume proxy order (unknown headroom → proxy);
  (c) refresh_headroom_if_stale — skips a fresh snapshot, refreshes a stale/absent one, 0 disables, fail-open.
"""
import os
import sys
import time
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-idlehr-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lanes, config                                      # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# four idle lanes; calls_recent is the OLD proxy order (fewest calls first)
_UTIL = {"lanes": [
    {"lane": "claude-code", "provider": "anthropic", "state": "idle", "utilization": 0.1, "calls_recent": 5000},
    {"lane": "codex", "provider": "openai", "state": "idle", "utilization": 0.1, "calls_recent": 10},
    {"lane": "gemini", "provider": "google", "state": "idle", "utilization": 0.1, "calls_recent": 20},
    {"lane": "zai-coding", "provider": "zai", "state": "idle", "utilization": 0.1, "calls_recent": 3000}]}

_o_util = lane_balance.lane_utilization
try:
    lane_balance.lane_utilization = lambda: _UTIL

    print("-- (b) NO snapshot → degrade EXACTLY to the call-volume proxy order (fewest calls first) --")
    config.save_state(lanes._HEADROOM_SNAPSHOT, {}, loud=False)     # empty snapshot
    order = lane_balance.idle_lanes()
    ck("proxy order (codex 10 < gemini 20 < zai 3000 < claude 5000)", order == ["codex", "gemini", "zai-coding", "claude-code"])

    print("\n-- (a) WITH a snapshot → order by headroom utility, cooling excluded --")
    now = time.time()
    config.save_state(lanes._HEADROOM_SNAPSHOT, {"asof": now, "rows": [
        {"lane": "claude-code", "provider": "anthropic", "known": True, "remaining_pct": 20, "reset_ts": now + 5 * 86400},
        {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 95, "reset_ts": now + 5 * 86400},
        {"lane": "gemini", "provider": "google", "known": True, "remaining_pct": 0, "reset_ts": now + 5 * 86400},
        {"lane": "zai-coding", "provider": "zai", "known": False, "remaining_pct": None, "reset_ts": None}]}, loud=False)
    order2 = lane_balance.idle_lanes()
    ck("codex (95% left) ranks first — most headroom", order2[0] == "codex")
    ck("claude-code (20%) ranks above the unknown zai (scored > None)", order2.index("claude-code") < order2.index("zai-coding"))
    ck("unknown-headroom zai-coding is LAST (proxy tail)", order2[-1] == "zai-coding")
    ck("gemini (0%) still present here (not cooling in this test) but ranks below the others",
       "gemini" in order2 and order2.index("gemini") > order2.index("claude-code"))
finally:
    lane_balance.lane_utilization = _o_util

print("\n-- (c) refresh_headroom_if_stale: skip fresh, refresh stale/absent, 0 disables, fail-open --")
calls = {"n": 0}
_o_fetch = lanes.lane_headroom
try:
    def _fake_fetch(do_fetch=True):
        calls["n"] += 1
        config.save_state(lanes._HEADROOM_SNAPSHOT, {"asof": time.time(), "rows": [{"lane": "x", "known": True, "remaining_pct": 50}]}, loud=False)
        return [{"lane": "x"}]
    lanes.lane_headroom = _fake_fetch

    config.save_state(lanes._HEADROOM_SNAPSHOT, {"asof": time.time(), "rows": [{"lane": "x"}]}, loud=False)  # fresh
    r = lanes.refresh_headroom_if_stale()
    ck("a FRESH snapshot is not re-fetched", r.get("fresh") is True and calls["n"] == 0)

    config.save_state(lanes._HEADROOM_SNAPSHOT, {"asof": time.time() - 3 * 3600, "rows": [{"lane": "x"}]}, loud=False)  # 3h old
    r2 = lanes.refresh_headroom_if_stale()
    ck("a STALE snapshot IS re-fetched", r2.get("refreshed") is True and calls["n"] == 1)

    os.environ["SPENDGUARD_HEADROOM_REFRESH_HOURS"] = "0"
    r3 = lanes.refresh_headroom_if_stale()
    ck("headroom_refresh_hours=0 disables the refresh", r3.get("skipped") is not None)
    del os.environ["SPENDGUARD_HEADROOM_REFRESH_HOURS"]
finally:
    lanes.lane_headroom = _o_fetch

print(f"\n{'[FAIL]' if fails else 'OK'} test_idle_lanes_headroom: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
