"""Cat-10 config/misc correctness:

  * brief._defaults computed a per-intent quality bar (qbar) but NEVER put it in the returned dict, so the
    renderer's `d.get('qbar', <generic fallback>)` ALWAYS showed the generic fallback — a headline "six" field
    (quality-bar+verification) silently broken. qbar is now returned.
  * deid._WARNED was dead: defined once, referenced nowhere (a leftover after warn-once moved to config's single
    registry). Removed; guarded here so it can't creep back.

Offline, isolated home; empty ledger.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat10-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import brief, deid        # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


d = brief._defaults("an-unseen-intent-xyz", "summarize a document")
ck("brief._defaults now RETURNS qbar (it was computed then dropped)", "qbar" in d)
ck("with no quality labels, qbar is the computed UNVERIFIED bar (not the renderer's generic fallback)",
   isinstance(d.get("qbar"), str) and d["qbar"].startswith("UNVERIFIED"), f"got {d.get('qbar')!r}")

ck("deid._WARNED dead global is gone", not hasattr(deid, "_WARNED"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat10_config_misc: {fails} failure(s)")
sys.exit(1 if fails else 0)
