"""Four verified-high ROBUSTNESS defects from the 4-LLM review, pinned:

  schedule._linux  — `crontab -l` failing for a REAL reason (permissions, daemon) returns empty stdout just like
                     "no crontab for user"; the old code treated both as empty and ran `crontab -`, WIPING the
                     user's existing jobs. Now it aborts on an ambiguous failure and never rewrites.
  realized.sync_to_guarded — after_calls = COUNT(ts >= adopted_ts); subtracting a watermark measured against a
                     DIFFERENT adoption point over-credits. A changed window now rebaselines and credits nothing.
  resources._all_instances — end_date/last_seen arrive as epoch floats OR strings across sources; `ed > ls` on
                     mixed types raised. The comparison is now crash-safe.
  brief.brief      — a `primary` recommendation can carry per=None; formatting None with %.4f raised. Guarded.

Offline, isolated home.
"""
import os
import sys
import tempfile
import types
import io
import contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-robust-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import schedule, realized, resources, brief, guard   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. schedule: a real crontab -l failure must NOT wipe the user's crontab ──────────────────────────────────
calls = []


def _fake_run(cmd, **kw):
    calls.append(list(cmd))
    if cmd[:2] == ["crontab", "-l"]:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="crontab: permission denied")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


schedule.subprocess.run = _fake_run
r = schedule._linux("daily", remove=False)
ck("crontab -l failing (not 'no crontab') → aborts with an error", isinstance(r, dict) and bool(r.get("error")))
ck("...and CRUCIALLY never runs `crontab -` to rewrite (no wipe of existing jobs)", ["crontab", "-"] not in calls)

calls.clear()


def _fake_run_empty(cmd, **kw):
    calls.append(list(cmd))
    if cmd[:2] == ["crontab", "-l"]:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for user")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


schedule.subprocess.run = _fake_run_empty
schedule._linux("daily", remove=False)
ck("a genuine 'no crontab' user still proceeds to install (crontab - IS run)", ["crontab", "-"] in calls)


# ── 2. realized: a changed adoption window credits nothing (no over-credit) ───────────────────────────────────
saved = []
guard.record_saving = lambda src, amount, project=None: saved.append((src, round(amount, 6)))
realized._load_state = lambda: {"intentX": {"counted_calls": 100, "adopted_ts": 1000}}
realized.sync_to_guarded(rows=[{"intent": "intentX", "delta_per_call": 0.01, "after_calls": 300, "adopted_ts": 2000}])
ck("a CHANGED adoption window credits NOTHING (rebaseline, never over-credit)", saved == [], str(saved))

saved.clear()
realized._load_state = lambda: {"intentY": {"counted_calls": 100, "adopted_ts": 1000}}
realized.sync_to_guarded(rows=[{"intent": "intentY", "delta_per_call": 0.01, "after_calls": 300, "adopted_ts": 1000}])
ck("the SAME window credits only the NEW calls (200 × $0.01 = $2.00)",
   len(saved) == 1 and abs(saved[0][1] - 2.0) < 1e-9, str(saved))


# ── 3. resources: mixed-type end_date/last_seen does not crash ───────────────────────────────────────────────
resources.instances = lambda: []
resources._load_history = lambda: {"box1": {"last_seen": 1000.0, "end_date": "2026-08-01", "id": "box1"}}
crashed = False
try:
    resources._all_instances()
except TypeError:
    crashed = True
ck("_all_instances handles a string end_date vs float last_seen without a TypeError", not crashed)


# ── 4. brief: a recommendation with per=None does not crash the format ───────────────────────────────────────
brief._defaults = lambda intent, task: {"rec": {"model": "gpt-x", "per": None, "good": None}}
crashed2, out = False, ""
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        brief.brief("do a thing", intent="myintent")
    out = buf.getvalue()
except TypeError:
    crashed2 = True
ck("brief() with a per=None recommendation does not crash formatting None", not crashed2)
ck("...and shows the cost as n/a", "cost n/a" in out, out[-120:])

print(f"\n{'[FAIL]' if fails else 'OK'} test_robustness_fixes: {fails} failure(s)")
sys.exit(1 if fails else 0)
