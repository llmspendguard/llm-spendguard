"""Cross-lane QUOTA view: the shared cache + headroom collapse (lane_quota), the claude-code /usage parser
(subscription_exec), and the per-lane aggregator (lanes.lane_headroom). Each lane executor exposes usage() →
buckets [{bucket, remaining_pct, reset_ts}] (provider truth where exposed, None where not); this proves the
provider-agnostic plumbing that turns those into a headroom view.

Pins (offline; no CLI, no network — CLIs/subprocess stubbed):
  (a) cached_usage — TTL cache, the reset-boundary invalidation (a refilled quota is never masked), None cached,
      fetch-exception → None (fail safe);
  (b) bucket_headroom — MIN remaining across buckets, soonest reset among the binding (min) buckets, None for
      None/empty (UNKNOWN ≠ 0%);
  (c) the claude-code parser — '<label>: N% used · resets <date>' → remaining = 100 - N, with the human date parsed;
  (d) lanes.lane_headroom — aggregates enabled lanes, marks known vs UNKNOWN, never breaks on one lane's error.
"""
import os
import sys
import time
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanequota-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_quota, subscription_exec as sx, lanes                        # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- (a) cached_usage: TTL cache, reset-boundary invalidation, None cached, fetch-exception → None --")
calls = {"n": 0}


def _fetch_ok():
    calls["n"] += 1
    return [{"bucket": "b", "remaining_pct": 50, "reset_ts": time.time() + 3600}]


cache = {"at": 0.0, "val": None}
u1 = lane_quota.cached_usage(cache, 300, _fetch_ok)
u2 = lane_quota.cached_usage(cache, 300, _fetch_ok)
ck("two reads within TTL fetch ONCE (cached)", calls["n"] == 1 and u1 == u2)

calls["n"] = 0
now = time.time()
cache2 = {"at": now, "val": [{"bucket": "b", "remaining_pct": 0, "reset_ts": now - 5}]}   # fresh by TTL, past its reset
_ = lane_quota.cached_usage(cache2, 300, _fetch_ok)
ck("a cached snapshot PAST its reset is refetched (refilled quota never masked)", calls["n"] == 1)


def _fetch_raises():
    raise RuntimeError("boom")


cache3 = {"at": 0.0, "val": None}
ck("a fetch that RAISES → None (fail safe, not a crash)", lane_quota.cached_usage(cache3, 300, _fetch_raises) is None)
ck("...and the None is cached (a burst does not re-hammer)", cache3["val"] is None and cache3["at"] > 0)

print("\n-- (b) bucket_headroom: MIN remaining, soonest reset among the binding buckets, None for unknown --")
now = time.time()
b = [{"bucket": "session", "remaining_pct": 88, "reset_ts": now + 100},
     {"bucket": "week", "remaining_pct": 40, "reset_ts": now + 9000},
     {"bucket": "week2", "remaining_pct": 40, "reset_ts": now + 5000}]
hr = lane_quota.bucket_headroom(b)
ck("remaining_pct is the MINIMUM (the tightest bucket)", hr["remaining_pct"] == 40)
ck("reset_ts is the SOONEST among the buckets AT that minimum (5000, not the 9000 sibling or the 100 non-binding)",
   abs(hr["reset_ts"] - (now + 5000)) < 1)
ck("None buckets → None (quota UNKNOWN, not 0%)", lane_quota.bucket_headroom(None) is None)
ck("empty buckets → None", lane_quota.bucket_headroom([]) is None)
ck("a bucket missing remaining_pct is treated as full (never invents scarcity)",
   lane_quota.bucket_headroom([{"bucket": "x"}])["remaining_pct"] == 100)

print("\n-- (c) claude-code parser: 'N% used · resets <date>' → remaining = 100-N, human date parsed --")
_SAMPLE = ("Current session: 12% used · resets Aug 28 at 12:10am (America/Los_Angeles)\n"
           "Current week (all models): 7% used · resets Sep 3 at 9am (America/Los_Angeles)\n"
           "Current week (Fable): 0% used\n"
           "⚠ some unrelated warning line with no percent\n")
rows = sx._parse_usage_claude(_SAMPLE)
ck("parsed three quota buckets (the warning line ignored)", rows is not None and len(rows) == 3)
sess = next((r for r in rows if "session" in r["bucket"].lower()), None)
ck("'12% used' → remaining 88%", sess is not None and sess["remaining_pct"] == 88)
ck("...and its reset date parsed to a unix ts", sess is not None and isinstance(sess["reset_ts"], float) and sess["reset_ts"] > time.time())
fable = next((r for r in rows if "fable" in r["bucket"].lower()), None)
ck("a bucket with no 'resets' → remaining 100%, reset_ts None", fable is not None and fable["remaining_pct"] == 100 and fable["reset_ts"] is None)
ck("a percent-less blob → None (no false bucket)", sx._parse_usage_claude("no percentages here") is None)

print("\n-- (d) lanes.lane_headroom: aggregates enabled lanes, marks known vs UNKNOWN, one lane's error can't break it --")
_o_status, _o_mods = lanes.status, lanes._lane_mods
try:
    lanes.status = lambda: {"lanes": [
        {"lane": "claude-code", "provider": "anthropic", "enabled": True},
        {"lane": "gemini", "provider": "google", "enabled": True},
        {"lane": "codex", "provider": "openai", "enabled": True},
        {"lane": "zai-coding", "provider": "zai", "enabled": True},
        {"lane": "off-lane", "provider": "x", "enabled": False}]}

    class _Good:
        @staticmethod
        def usage():
            return [{"bucket": "w", "remaining_pct": 90, "reset_ts": None}]

    class _Exhausted:
        @staticmethod
        def usage():
            return [{"bucket": "w", "remaining_pct": 0, "reset_ts": time.time() + 60}]

    class _NoSurface:            # provider exposes nothing → usage() returns None (UNKNOWN)
        @staticmethod
        def usage():
            return None

    class _Boom:                 # a lane whose usage() RAISES must not break the whole view
        @staticmethod
        def usage():
            raise RuntimeError("nope")

    lanes._lane_mods = lambda: {"claude-code": _Good, "gemini": _Exhausted, "codex": _NoSurface, "zai-coding": _Boom}
    hm = {h["lane"]: h for h in lanes.lane_headroom()}
    ck("only ENABLED lanes appear (off-lane excluded)", "off-lane" not in hm and len(hm) == 4)
    ck("a lane with quota → known + its remaining%", hm["claude-code"]["known"] and hm["claude-code"]["remaining_pct"] == 90)
    ck("an exhausted lane → known + 0% (NOT unknown)", hm["gemini"]["known"] and hm["gemini"]["remaining_pct"] == 0)
    ck("a no-surface lane → known=False, remaining None (UNKNOWN)", hm["codex"]["known"] is False and hm["codex"]["remaining_pct"] is None)
    ck("a lane whose usage() RAISED → UNKNOWN, not a crash", hm["zai-coding"]["known"] is False)
finally:
    lanes.status, lanes._lane_mods = _o_status, _o_mods

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_quota: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
