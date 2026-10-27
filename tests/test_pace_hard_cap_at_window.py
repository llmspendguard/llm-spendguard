"""The PACE engine must ENFORCE the window cap, not just nudge. 'Use each plan to ~100% of its window, never over'
means a lane at/below its reserve of remaining capacity is SHED (available=False) — otherwise an exhausted lane
still scores >= 1.0, OUTRANKS metered, and fungible work keeps hitting it past 100% (wasted failed attempts, and real
overage on a plan that bills instead of blocking, booked as $0). Measured 2026-09-01: a 0%-remaining lane ranked #1.

Pins:
  1. a 0% (exhausted) lane, not protected, not cooling → SHED at the window cap (not merely un-boosted);
  2. it sorts BELOW available lanes (never #1);
  3. a lane with real headroom is available; a proxy lane (unknown remaining) is NEVER capped (a can't-know is not a no);
  4. a positive reserve holds back a margin (stop before 100%).

Hermetic: synthetic rows + explicit cooling/protect/pace; the reserve reader is monkeypatched. Zero spend."""
import sys

from _script_checks import record_script_check
from spendguard import route_utility as ru
import spendguard.lane_economics as le

fails = []
NOW = 1_000_000_000.0
DAY = 86400.0
def _row(lane, pct):
    return {"lane": lane, "provider": "p", "known": True, "remaining_pct": pct,
            "remaining_abs": None, "reset_ts": NOW + 3 * DAY, "buckets": []}

_orig = le.pace_reserve_frac

# ── reserve 0.0 (use fully; shed only at exactly 0%) ────────────────────────────────────────────────────────────
le.pace_reserve_frac = lambda lane: 0.0
try:
    ranked = ru.rank_lanes([_row("exhausted", 0), _row("half", 50), _row("proxy", None)],
                           cooling=lambda l: False, protect=lambda l: False, now=NOW,
                           pace_by={"exhausted": -0.5, "half": 0.2, "proxy": None})
    by = {d["lane"]: d for d in ranked}
    record_script_check(fails, "0% (exhausted) lane → SHED at the window cap", by["exhausted"]["available"] is False and "window cap" in by["exhausted"]["why"])
    record_script_check(fails, "exhausted lane sorts LAST (never #1 over an available lane)", ranked[-1]["lane"] == "exhausted")
    record_script_check(fails, "a lane with real headroom (50%) stays available", by["half"]["available"] is True)
    record_script_check(fails, "a proxy lane (unknown remaining) is NEVER capped (can't-know is not a no)", by["proxy"]["available"] is True)
finally:
    le.pace_reserve_frac = _orig

# ── a positive reserve holds back a margin (stop before 100%) ───────────────────────────────────────────────────
le.pace_reserve_frac = lambda lane: 0.15
try:
    r2 = {d["lane"]: d for d in ru.rank_lanes([_row("low", 10), _row("plenty", 80)],
                                              cooling=lambda l: False, protect=lambda l: False, now=NOW,
                                              pace_by={"low": -0.1, "plenty": 0.1})}
    record_script_check(fails, "reserve 0.15 → a 10%-left lane is shed (held-back margin)", r2["low"]["available"] is False)
    record_script_check(fails, "reserve 0.15 → an 80%-left lane is still available", r2["plenty"]["available"] is True)
finally:
    le.pace_reserve_frac = _orig

# reserve reader itself: default 0.0, clamped
record_script_check(fails, "pace_reserve_frac default is 0.0 (use fully)", le.pace_reserve_frac("some-unconfigured-lane") == 0.0)

print(("\n[OK] " if not fails else "\n[FAIL] ") + "pace_hard_cap_at_window: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
