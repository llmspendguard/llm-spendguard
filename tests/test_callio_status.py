"""`spendguard callio-status` — the corpus-fill readout you consult BEFORE estimating a sweep.

The count that matters for readiness is not `sampled` but `replayable` (truncated=0): bakeoff/effort-titrate sample
ONLY non-truncated rows, so an intent with 50 samples but all truncated (the old 800-char judge snip) can't run a
sweep. This guards that status_rows reports sampled / replayable / live / judged correctly per (intent, model), and
that the --intent substring filter works.

Offline + hermetic: temp HOME, no network.
"""
import os
import sys
import tempfile

import atexit as _atexit   # noqa: E402
import shutil as _shutil   # noqa: E402
_SG_HOME = tempfile.mkdtemp(prefix="sg-status-")
os.environ["SPENDGUARD_HOME"] = _SG_HOME
_atexit.register(_shutil.rmtree, _SG_HOME, ignore_errors=True)   # clean up the temp HOME — don't leak it into $TMPDIR
os.environ["SPENDGUARD_CAPTURE_LIVE"] = "1"               # so capture_live records a live row below
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import callio   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []

# intentA/m1: one FULL batch row (replayable) + one TRUNCATED batch row (prompt > the 800 snip → truncated=1, excluded)
callio.record_io_sample("intentA", "openai", "m1", "b1", "c1", "short batch prompt", "out")
callio.record_io_sample("intentA", "openai", "m1", "b2", "c2", "P" * 900, "out")
# intentB/m2: one LIVE row (source live_io, whole → replayable)
callio.capture_live("intentB", "openai", "m2", "a live workload prompt", "out")

by = {(d["intent"], d["model"]): d for d in callio.status_rows()}

print("-- sampled counts every row; replayable counts only truncated=0 (what a sweep can sample) --")
_a = by.get(("intentA", "m1"))
fails += report_check("intentA/m1: sampled=2", _a and _a["sampled"] == 2)
fails += report_check("intentA/m1: replayable=1 (the truncated 900-char row is EXCLUDED)", _a and _a["replayable"] == 1)
fails += report_check("intentA/m1: live=0 (both were batch rows)", _a and _a["live"] == 0)

print("\n-- a LIVE-captured row is counted as replayable AND flagged live --")
_b = by.get(("intentB", "m2"))
fails += report_check("intentB/m2: sampled=1, replayable=1, live=1",
                      _b and _b["sampled"] == 1 and _b["replayable"] == 1 and _b["live"] == 1)

print("\n-- the --intent substring filter keeps only matching intents --")
only_b = callio.status_rows(["intentB"])
fails += report_check("filter 'intentB' returns only intentB rows",
                      bool(only_b) and all(d["intent"] == "intentB" for d in only_b))
fails += report_check("filter 'nope' returns nothing (no match)", callio.status_rows(["nope-xyz"]) == [])

print("\n-- status_main renders without error ($0) --")
fails += report_check("status_main() returns 0", callio.status_main([]) == 0)
fails += report_check("status_main(--json) returns 0", callio.status_main(["--json"]) == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_callio_status: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
