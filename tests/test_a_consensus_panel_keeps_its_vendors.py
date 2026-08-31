"""A cross-vendor consensus panel must be able to KEEP its vendors.

THE DEFECT THIS LOCKS OUT, measured 2026-08-29 on a warden S1 review. With `advisor.bandit_mode=optout` and an
empty `advisor.bandit_denylist`, EVERY intent was eligible for lane substitution — so honestreview's five-vendor
panel ran `openai:gpt-5.5` in place of gemini (9x), zai (7x), moonshot (7x) and anthropic (7x). The panel's report
still printed `anth=ok,moon=ok,gemi=ok` per file, so a "5-vendor consensus" was ONE model agreeing with itself
while every agreement count in the output claimed four independent vendors had concurred.

Substituting the model is exactly right for work that needs AN answer, and exactly wrong for work where WHICH
MODEL ANSWERED is the measurement. The denylist is the sanctioned channel for the second case — but it was
exact-match, and a panel labels each call `review:<filename>`, so covering it would have meant enumerating every
file in every repo forever. The channel did not exist for the caller that most needed it.

Offline: pure list matching plus a read of the deployed config. No LLM, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-panel-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard.lane_balance import _intent_listed as listed                            # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return [] if ok else [name]


# A PREFIX ENTRY COVERS A PER-ITEM INTENT FAMILY — the whole point, since the names are generated per work item.
fails += ck("`review:` covers review:catalog.py", listed("review:catalog.py", ["review:"]))
fails += ck("...and every other member of the family", listed("review:evidence.py", ["review:"]))
fails += ck("the star form reads the same", listed("honestreview:panel", ["honestreview:*"]))
fails += ck("an unrelated intent is untouched", not listed("warden:describe", ["review:"]))

# AN EXACT ENTRY MUST NOT SILENTLY BECOME A PREFIX. Every existing config lists exact intent names; if those
# quietly started matching by prefix, `warden:describe` would capture `warden:describe_fields` and change routing
# for work nobody denied. A prefix is OPT-IN via a trailing ':' or '*' — parsing a known shape, not guessing.
fails += ck("an exact entry still matches exactly", listed("warden:describe", ["warden:describe"]))
fails += ck("...and does NOT capture a longer sibling", not listed("warden:describe_fields", ["warden:describe"]))

# THE EMPTY CASE DECIDES THE DEFAULT POSTURE AND MUST NOT INVERT. In `optout` mode eligibility is
# `not _intent_listed(...)`, so a helper returning True on an empty list would deny every intent and silently
# switch the bandit off estate-wide — the opposite failure, equally quiet.
fails += ck("an unset list lists nothing", not listed("anything", None))
fails += ck("an empty list lists nothing", not listed("anything", []))

# THE MECHANISM IS ONLY HALF THE FIX — the panel has to actually be on the list. The 2026-08-29 collapse happened
# with a perfectly good denylist mechanism that had nothing in it, so the DEPLOYED posture is checked too.
#
# READ THE REAL FILE, not `config._cfg_get`. This module runs under an isolated SPENDGUARD_HOME (a fresh temp dir,
# correct for every check above), so the config module would answer from an EMPTY config and the check would fail
# for a reason that has nothing to do with the deployed posture — a test failing for the wrong reason teaches the
# reader to ignore it. Only the real file can answer "is the panel protected on this machine".
#
# ABSENT ⇒ REPORTED, NOT PASSED. On a machine with no config (CI, a fresh clone) there is no posture to check;
# saying so is honest, whereas returning green would be this repo's own "cannot tell is not clean" failure.
import json as _json                                                                    # noqa: E402
import pathlib as _pathlib                                                              # noqa: E402

_live = _pathlib.Path(os.path.expanduser("~/.spendguard/config.json"))
if not _live.exists():
    print(f"  n/a  no deployed config at {_live} — deployed posture UNCHECKED (not a pass)")
else:
    try:
        _deny = (_json.loads(_live.read_text()).get("advisor") or {}).get("bandit_denylist") or []
    except Exception as e:                  # a config that cannot be read is NOT a pass
        _deny, _ = [], print(f"  FAIL live config unreadable: {type(e).__name__}")
    fails += ck(f"the DEPLOYED config denies the consensus panel (denylist={_deny})",
                listed("review:catalog.py", _deny))

print(f"\n{'[FAIL]' if fails else 'OK'} a_consensus_panel_keeps_its_vendors: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
