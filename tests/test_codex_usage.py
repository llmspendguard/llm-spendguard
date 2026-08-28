"""codex lane QUOTA — read from the rate-limit events codex records in its logs sqlite (the ChatGPT plan has no
status command, and `codex exec --json` emits only token counts). Pins:
  (a) _parse_rate_limits — the fixed codex.rate_limits schema → buckets (used_percent → remaining, reset_at absolute,
      reset_after_seconds fallback, a PAST reset rolled forward by whole windows, None when no bucket);
  (b) _fetch_usage picks the FRESHEST event by its own `ts` column across every logs*.sqlite — NOT the file mtime,
      so a restore/sync/`touch` that scrambles mtimes cannot make a stale logs_1 outrank the current logs_2.
Offline: fake logs sqlites in a temp CODEX_HOME; no CLI, no network.
"""
import os
import sys
import time
import json
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-codexq-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec as cx                                                 # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- (a) _parse_rate_limits: the fixed codex.rate_limits schema → buckets --")
now = time.time()
payload = {"type": "codex.rate_limits", "rate_limits": {
    "primary": {"used_percent": 12, "window_minutes": 300, "reset_at": now + 3600},
    "secondary": {"used_percent": 0, "window_minutes": 10080, "reset_at": now + 500000}}}
b = cx._parse_rate_limits(payload)
ck("primary + secondary → two buckets", b is not None and len(b) == 2)
prim = next((x for x in b if x["bucket"].startswith("primary")), None)
ck("used_percent 12 → remaining 88", prim is not None and prim["remaining_pct"] == 88)
ck("reset_at is taken as an absolute unix ts", prim is not None and abs(prim["reset_ts"] - (now + 3600)) < 1)

ck("used_percent 0 (secondary) → remaining 100",
   next(x["remaining_pct"] for x in b if x["bucket"].startswith("secondary")) == 100)

# reset_after_seconds fallback when reset_at is absent
b2 = cx._parse_rate_limits({"rate_limits": {"primary": {"used_percent": 5, "window_minutes": 60, "reset_after_seconds": 1800}}})
ck("no reset_at → reset_after_seconds gives a ~now+that ts", abs(b2[0]["reset_ts"] - (now + 1800)) < 5)

# a PAST reset with a known window rolls FORWARD to the next boundary (a stale event never shows a past date)
past = now - (300 * 60) * 3 - 100          # 3-and-a-bit 300-minute windows ago
b3 = cx._parse_rate_limits({"rate_limits": {"primary": {"used_percent": 0, "window_minutes": 300, "reset_at": past}}})
ck("a PAST reset_at is rolled forward to a FUTURE boundary", b3[0]["reset_ts"] > now)
ck("...by a whole number of windows (aligned to the original)", abs((b3[0]["reset_ts"] - past) % (300 * 60)) < 1)

ck("no primary/secondary → None", cx._parse_rate_limits({"rate_limits": {"primary": None, "secondary": None}}) is None)
ck("a bucket missing used_percent is skipped", cx._parse_rate_limits({"rate_limits": {"primary": {"window_minutes": 60}}}) is None)


def _make_logs_db(path, ts, used_percent):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, feedback_log_body TEXT)")
    body = "Received message " + json.dumps({"type": "codex.rate_limits", "rate_limits": {
        "primary": {"used_percent": used_percent, "window_minutes": 10080, "reset_at": time.time() + 500000}}})
    con.execute("INSERT INTO logs (ts, feedback_log_body) VALUES (?, ?)", (ts, body))
    con.commit()
    con.close()


print("\n-- (b) _fetch_usage: freshest by EVENT ts across all logs*.sqlite, NOT file mtime (the durability fix) --")
home = tempfile.mkdtemp(prefix="codexhome-")
db_old_event = os.path.join(home, "logs_1.sqlite")     # OLDER event ts, but we will make its FILE mtime NEWER
db_new_event = os.path.join(home, "logs_2.sqlite")     # NEWER event ts, FILE mtime OLDER
_make_logs_db(db_old_event, ts=1000, used_percent=50)  # → would give remaining 50
_make_logs_db(db_new_event, ts=2000, used_percent=10)  # → the freshest event; remaining 90
os.utime(db_old_event, (time.time(), time.time()))     # make the STALE-event file the newest by MTIME (the trap)
os.utime(db_new_event, (time.time() - 9999, time.time() - 9999))

_o_home = cx._codex_home
try:
    cx._codex_home = lambda: home
    cx._usage_cache["at"], cx._usage_cache["val"] = 0.0, None       # bypass any cache
    u = cx._fetch_usage()
    ck("the freshest-EVENT row wins (remaining 90), though its file has the OLDER mtime",
       u is not None and u[0]["remaining_pct"] == 90)

    # no logs db at all → None (fail safe)
    cx._codex_home = lambda: tempfile.mkdtemp(prefix="empty-")
    ck("no logs sqlite → None (quota UNKNOWN, not a crash)", cx._fetch_usage() is None)
finally:
    cx._codex_home = _o_home

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_usage: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
