"""gate.py — the realtime-by-day aggregation + the `spendguard` gate CLI surface (status/doctor/off/on). The core
enforcement DECISIONS are covered by test_gate.py; this covers the reporting + control surface (the biggest still-
uncovered block). Offline, isolated home. Script-style."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gatecli-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── realtime_by_day: empty when no log, then aggregates by day + by model, honoring `since` ──
ck("realtime_by_day: no log file → ({}, {})", gate.realtime_by_day() == ({}, {}))

os.makedirs(os.path.dirname(gate.RT_LOG), exist_ok=True)
with open(gate.RT_LOG, "w") as f:
    for r in [{"day": "2026-06-01", "cost": 1.50, "model": "gpt-5.5"},
              {"day": "2026-06-01", "cost": 0.50, "model": "claude-opus-4-8"},
              {"day": "2026-06-02", "cost": 2.00, "model": "gpt-5.5"},
              "",                                              # blank line → skipped
              "{not json}"]:                                  # malformed → skipped, no crash
        f.write((json.dumps(r) if isinstance(r, dict) else r) + "\n")

by_day, by_model = gate.realtime_by_day()
ck("realtime_by_day: by_day sums per day", abs(by_day.get("2026-06-01", 0) - 2.0) < 1e-9 and abs(by_day.get("2026-06-02", 0) - 2.0) < 1e-9)
ck("realtime_by_day: by_model sums per model", abs(by_model.get("gpt-5.5", 0) - 3.5) < 1e-9 and abs(by_model.get("claude-opus-4-8", 0) - 0.5) < 1e-9)
ck("realtime_by_day: malformed/blank lines are skipped, not fatal", "?" not in by_model or by_model.get("?", 0) == 0)
since = gate.realtime_by_day(since="2026-06-02")[0]
ck("realtime_by_day: `since` filters earlier days out", "2026-06-01" not in since and abs(since.get("2026-06-02", 0) - 2.0) < 1e-9)

# ── _cli off/on toggles the persistent flag; status/doctor return 0 ──
ck("_cli('off') writes the kill-switch flag → gate disabled", gate._cli("off") == 0 and gate._disabled() is True and os.path.exists(gate.FLAG))
ck("_cli('on') clears the flag → gate enabled", gate._cli("on") == 0 and gate._disabled() is False and not os.path.exists(gate.FLAG))
ck("_cli('status') returns 0 (prints the enforcement report)", gate._cli("status") == 0)
ck("_cli() default is status, returns 0", gate._cli() == 0)

# ── doctor is a HEALTH CHECK: fast, cached leak verdict, honest UNKNOWN (incident #25) ──
import io
import time
import contextlib
from spendguard import ledger_sync

buf = io.StringIO()
t0 = time.monotonic()
with contextlib.redirect_stdout(buf):
    rc = gate._cli("doctor")
took = time.monotonic() - t0
ck("_cli('doctor') returns 0", rc == 0)
ck("doctor completes fast (<2s) — never live-pulls provider billing by default", took < 2.0)
ck("no cache → leak status reads UNKNOWN (never a silent skip)", "UNKNOWN" in buf.getvalue())

# a prior report/reconcile computed the leak line → doctor reuses it WITH ITS AGE
ledger_sync._compute = lambda since=None: {"post_p": 100.0, "leak": 0.0, "capture_rate": 95.0,
                                           "pre_ledger": 0.0, "coverage": 100.0, "cutoff": "2026-06-01"}
line = ledger_sync.leak_line()
ck("leak_line persists its verdict as a byproduct", line and ledger_sync._leak_cache_path().exists())
got = ledger_sync.cached_leak_line()
ck("cached_leak_line returns (line, age)", got and got[0] == line and got[1] < 60)
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    gate._cli("doctor")
ck("doctor shows the CACHED verdict with its age", "as of" in buf2.getvalue() and "accounted" in buf2.getvalue())
buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    gate._cli("doctor", live=True)
ck("doctor --live forces the full computation", "accounted" in buf3.getvalue() and "as of" not in buf3.getvalue())

print(("\n[FAIL] " if fails else "\n[OK] ") + f"gate_cli: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
