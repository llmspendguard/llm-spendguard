"""A DEAD bandit-list entry (matching no recorded intent) must be VISIBLE, not silent. Script-style, offline.

The warden incident (2026-08-29): a live bandit_denylist named `warden:card_faithful` and `warden:crosscheck`,
both with ZERO rows in the calls ledger — so the denylist protected nothing and nothing said so (an
absence-read-as-success at the config layer, axis-4). bandit_list_coverage() surfaces it; `spendguard doctor`
prints it. This guards that a dead entry is flagged AND that a live PREFIX entry is not a false positive.
"""
import os
import sys
import tempfile
import json
import pathlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-denylist-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
# a denylist with ONE prefix that WILL match a recorded intent, and two dead exact names (the warden incident)
pathlib.Path(os.environ["SPENDGUARD_HOME"], "config.json").write_text(json.dumps(
    {"advisor": {"bandit_denylist": ["warden:gold*", "warden:card_faithful", "warden:crosscheck"]}}))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, calls

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# record a real call whose intent the 'warden:gold*' prefix should cover
calls.insert("openai", "gpt-5.5", "realtime", 0.01, intent="warden:gold_adjudicate")

cov = lane_balance.bandit_list_coverage()
d = cov.get("bandit_denylist", {})
ck("a readable ledger reports read_ok=True", d.get("read_ok") is True)
ck("the live PREFIX entry 'warden:gold*' is NOT unmatched (it matches warden:gold_adjudicate)", "warden:gold*" not in d.get("unmatched", []))
ck("the two entries matching NO recorded intent are flagged UNMATCHED",
   set(d.get("unmatched", [])) == {"warden:card_faithful", "warden:crosscheck"})
ck("coverage reports the seen-intent count it judged against", d.get("seen_n", 0) >= 1)

# a READ FAILURE must NOT false-flag every entry UNMATCHED ('cannot tell' != 'all unmatched')
import sqlite3 as _sq
_orig_connect = _sq.connect
_sq.connect = lambda *a, **k: (_ for _ in ()).throw(_sq.OperationalError("forced read failure"))
try:
    df = lane_balance.bandit_list_coverage().get("bandit_denylist", {})
finally:
    _sq.connect = _orig_connect
ck("a ledger read failure → read_ok=False, unmatched=None (never invents 'all unmatched')",
   df.get("read_ok") is False and df.get("unmatched") is None)

# once an intent for a formerly-unmatched entry IS recorded, it stops being unmatched (self-healing signal)
calls.insert("openai", "gpt-5.5", "realtime", 0.01, intent="warden:card_faithful")
d2 = lane_balance.bandit_list_coverage().get("bandit_denylist", {})
ck("an unmatched entry becomes matched once a matching intent is recorded",
   "warden:card_faithful" not in d2.get("unmatched", []) and d2.get("unmatched", []) == ["warden:crosscheck"])

print(("[OK]" if not fails else "[FAIL]") + " bandit list coverage: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
