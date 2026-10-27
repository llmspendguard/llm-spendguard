"""trust.verdict — the pure double-count / drift detector (provider billing vs recorded). Born from the 2x prod
incident: a recorded total far above provider truth must ALARM, and an unreadable bill must be UNKNOWN, never a
silent 'ok'. Offline, isolated home. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-trust-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import trust

fails = []
# the incident: recorded ~2x provider truth → ALARM
lvl, msg = trust.verdict(2232.0, 4788.0)
record_script_check(fails, "2x provider truth → ALARM (the prod incident)", lvl == "alarm" and "DOUBLE" in msg)

# recorded ≈ truth → ok
record_script_check(fails, "recorded ≈ truth → ok", trust.verdict(2232.0, 2240.0)[0] == "ok")
record_script_check(fails, "recorded within ±15% → ok", trust.verdict(1000.0, 1100.0)[0] == "ok")

# moderate over/under → warn (not alarm)
record_script_check(fails, "recorded +25% → warn", trust.verdict(1000.0, 1250.0)[0] == "warn")
record_script_check(fails, "recorded -30% → warn (under-recording is also a problem)", trust.verdict(1000.0, 700.0)[0] == "warn")

# exactly at the alarm ratio → alarm
record_script_check(fails, "ratio >= 1.4 → alarm", trust.verdict(1000.0, 1400.0)[0] == "alarm")

# UNKNOWN truth (fetch failed) must NOT read as ok — the silent-zero guard
record_script_check(fails, "truth=None → UNKNOWN (never silently ok)", trust.verdict(None, 4788.0)[0] == "unknown")
record_script_check(fails, "UNKNOWN message says do not trust", "do NOT trust" in trust.verdict(None, 1.0)[1])

# truth $0
record_script_check(fails, "truth 0 + recorded 0 → ok", trust.verdict(0.0, 0.0)[0] == "ok")
record_script_check(fails, "truth 0 but recorded >0 → warn (spend with no provider record)", trust.verdict(0.0, 50.0)[0] == "warn")

# thresholds are the documented constants
record_script_check(fails, "ALARM_RATIO + WARN_FRAC are the tuned thresholds", trust.ALARM_RATIO == 1.4 and trust.WARN_FRAC == 0.15)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"trust: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
