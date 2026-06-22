"""trust.verdict — the pure double-count / drift detector (provider billing vs recorded). Born from the 2x prod
incident: a recorded total far above provider truth must ALARM, and an unreadable bill must be UNKNOWN, never a
silent 'ok'. Offline, isolated home. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-trust-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import trust

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# the incident: recorded ~2x provider truth → ALARM
lvl, msg = trust.verdict(2232.0, 4788.0)
ck("2x provider truth → ALARM (the prod incident)", lvl == "alarm" and "DOUBLE" in msg)

# recorded ≈ truth → ok
ck("recorded ≈ truth → ok", trust.verdict(2232.0, 2240.0)[0] == "ok")
ck("recorded within ±15% → ok", trust.verdict(1000.0, 1100.0)[0] == "ok")

# moderate over/under → warn (not alarm)
ck("recorded +25% → warn", trust.verdict(1000.0, 1250.0)[0] == "warn")
ck("recorded -30% → warn (under-recording is also a problem)", trust.verdict(1000.0, 700.0)[0] == "warn")

# exactly at the alarm ratio → alarm
ck("ratio >= 1.4 → alarm", trust.verdict(1000.0, 1400.0)[0] == "alarm")

# UNKNOWN truth (fetch failed) must NOT read as ok — the silent-zero guard
ck("truth=None → UNKNOWN (never silently ok)", trust.verdict(None, 4788.0)[0] == "unknown")
ck("UNKNOWN message says do not trust", "do NOT trust" in trust.verdict(None, 1.0)[1])

# truth $0
ck("truth 0 + recorded 0 → ok", trust.verdict(0.0, 0.0)[0] == "ok")
ck("truth 0 but recorded >0 → warn (spend with no provider record)", trust.verdict(0.0, 50.0)[0] == "warn")

# thresholds are the documented constants
ck("ALARM_RATIO + WARN_FRAC are the tuned thresholds", trust.ALARM_RATIO == 1.4 and trust.WARN_FRAC == 0.15)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"trust: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
