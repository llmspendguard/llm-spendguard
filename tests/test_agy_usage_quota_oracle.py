"""agy `/usage` as the quota-reset ORACLE for the Gemini lane. agy reports Gemini quota exhaustion through the
print/JSON path as a bare, window-LESS ERROR (status='ERROR', empty response), so antigravity_exec._reset_window_s
finds no "resets in …" token and the lane would be blind-retried every ~13 min. `/usage` DOES expose the exact
per-bucket weekly reset; feeding it into retry_after_s lets the EXISTING transient path cool the lane until its
(capped, re-tested) reset instead of guessing.

Pins (offline; agy binary + subprocess stubbed, no CLI, no network):
  (a) _parse_usage extracts buckets from the real tab/percent/ISO shape (a PARSE, not a wording judgement);
  (b) _quota_reset_s is DECISION-FREE — it reads the quota NUMBERS and returns the SOONEST reset among 0%-remaining
      buckets, and None when every bucket has headroom (no model→bucket classification anywhere);
  (c) usage() CACHES — a burst of failures costs ONE $0 CLI round-trip;
  (d) _error_result attaches retry_after_s from /usage on a window-less error, and an INLINE window still wins.
"""
import os
import sys
import time
import tempfile
import datetime

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-agyusage-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import antigravity_exec as agy                                          # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _reset_cache():
    agy._usage_cache["at"], agy._usage_cache["val"] = 0.0, None


# real shape: "<bucket>\t<label>\t<pct>%\t<ISO8601>"
_SAMPLE = ("Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-30T19:26:10Z\n"
           "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-09-04T02:35:52Z\n")

print("-- (a) _parse_usage extracts {bucket, remaining_pct, reset_ts} from the real shape --")
rows = agy._parse_usage(_SAMPLE)
check("parsed two buckets", rows is not None and len(rows) == 2)
gem = next((r for r in (rows or []) if "gemini" in r["bucket"].lower()), None)
check("Gemini bucket at 0%", gem is not None and gem["remaining_pct"] == 0)
check("...with the ISO reset parsed to a unix ts",
      gem is not None and abs(gem["reset_ts"] - datetime.datetime.fromisoformat("2026-08-30T19:26:10+00:00").timestamp()) < 1)
check("a line without a %+timestamp is ignored (no false bucket)", agy._parse_usage("just some log line\n") is None)

print("\n-- (b) _quota_reset_s: SOONEST reset among EXHAUSTED buckets; None when all have headroom (decision-free) --")
_o_usage = agy.usage
try:
    now = time.time()
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now + 3600},
                         {"bucket": "Claude and GPT models", "remaining_pct": 100, "reset_ts": now + 10}]
    r = agy._quota_reset_s()
    check("one bucket exhausted → ~its reset (3600s), NOT the healthy bucket's sooner reset", 3590 <= r <= 3600)
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now + 5000},
                         {"bucket": "Claude and GPT models", "remaining_pct": 0, "reset_ts": now + 1200}]
    r2 = agy._quota_reset_s()
    check("two exhausted → the SOONEST reset (1200s, the earliest the lane can recover)", 1190 <= r2 <= 1200)
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 42, "reset_ts": now + 100},
                         {"bucket": "Claude and GPT models", "remaining_pct": 100, "reset_ts": now + 100}]
    check("all buckets have headroom → None (no cooldown, ordinary handling)", agy._quota_reset_s() is None)
    agy.usage = lambda: None
    check("no /usage (agy down/parse gap) → None (fails safe)", agy._quota_reset_s() is None)
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now - 50}]
    check("an already-past reset → None (not a negative cool)", agy._quota_reset_s() is None)
finally:
    agy.usage = _o_usage

print("\n-- (c) usage() CACHES: a burst of failures costs ONE $0 CLI round-trip --")
calls = {"n": 0}


class _CP:
    returncode = 0
    stdout = _SAMPLE


_o_bin, _o_run = agy._bin, agy.subprocess.run
try:
    agy._bin = lambda: "/fake/agy"
    def _fake_run(*a, **k):
        calls["n"] += 1
        return _CP()
    agy.subprocess.run = _fake_run
    _reset_cache()
    u1 = agy.usage()
    u2 = agy.usage()
    check("two usage() calls within the TTL invoked the CLI ONCE (cached)", calls["n"] == 1)
    check("...and both returned the parsed rows", u1 is not None and u1 == u2 and len(u1) == 2)
finally:
    agy._bin, agy.subprocess.run = _o_bin, _o_run
    _reset_cache()

print("\n-- (c2) a cached snapshot PAST its reported reset is REFETCHED (a refilled quota is never masked) --")
calls["n"] = 0
_o_bin2, _o_run2 = agy._bin, agy.subprocess.run
try:
    agy._bin = lambda: "/fake/agy"
    agy.subprocess.run = _fake_run
    now = time.time()
    # fresh by TTL, but its soonest reset already elapsed → the quota has refilled, so the cache must invalidate
    agy._usage_cache["at"] = now
    agy._usage_cache["val"] = [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now - 10}]
    u = agy.usage()
    check("past-reset cache → refetched despite the TTL (CLI invoked once)", calls["n"] == 1)
    check("...and returns the FRESH rows, not the stale exhausted snapshot",
          u is not None and len(u) == 2 and any("gemini" in r["bucket"].lower() for r in u))
finally:
    agy._bin, agy.subprocess.run = _o_bin2, _o_run2
    _reset_cache()

print("\n-- (d) _error_result: window-less quota error → retry_after_s from /usage; an INLINE window still wins --")
_o_usage2 = agy.usage
try:
    now = time.time()
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now + 2400}]
    er = agy._error_result("agy status='ERROR'")            # the real window-less quota envelope
    check("a window-less error gains retry_after_s from /usage", 2390 <= (er.get("retry_after_s") or 0) <= 2400)
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 100, "reset_ts": now + 2400}]
    er2 = agy._error_result("agy status='ERROR'")
    check("a window-less error with quota HEADROOM stays a plain error (no retry_after_s)", "retry_after_s" not in er2)
    agy.usage = lambda: [{"bucket": "Gemini Models", "remaining_pct": 0, "reset_ts": now + 999999}]
    er3 = agy._error_result("rate limited, try again in 45s")   # inline window present
    check("an INLINE reset window WINS over /usage (45s, not the weekly bucket)", er3.get("retry_after_s") == 45)
finally:
    agy.usage = _o_usage2

print(f"\n{'[FAIL]' if fails else 'OK'} test_agy_usage_quota_oracle: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
