"""Guard: the Stop-hook compaction nudge is an at-most-ONCE-per-crossing notice, never a per-turn interrupt, and
never claims a saving it cannot stand behind.

Locks out the nag loop reported 2026-10-06: the Stop hook fired ~15 consecutive identical nudges in one session
(each firing ended a turn, which fired it again), and the notice read "~1x cheaper" — a claim of a saving that is
identical cost. Three invariants, enforced here so they cannot silently return:
  1. over threshold, the nudge fires on the FIRST crossing and is then SILENT until the context grows materially;
  2. dropping back under the threshold (a compaction / fresh start) RESETS it, so the next crossing nudges once;
  3. the terse nudge carries NO "cheaper" savings claim at all (whatever the measured k×).
The passive status line (once=False) is not an interrupt and still shows the notice whenever over threshold.
"""
import os
import sys
import json
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-nudge-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ.setdefault("OPENAI_API_KEY", "sk-test-nudge-never-used")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import receipt  # noqa: E402

HOME = os.environ["SPENDGUARD_HOME"]
fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


def info_for(ctx_tokens, session="sess-A"):
    """A hook payload whose transcript's last usage line sums to ctx_tokens."""
    p = os.path.join(HOME, f"transcript_{session}_{ctx_tokens}.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"message": {"usage": {
            "input_tokens": ctx_tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}) + "\n")
    return {"transcript_path": p, "session_id": session}


# No compaction_hint.json seeded → threshold defaults to 100000, k is absent.
print("-- the Stop-hook nudge fires ONCE per crossing, not per turn --")
n1 = receipt._compaction_nudge(info_for(150000), HOME, once=True)
ck("first crossing over threshold fires a nudge", bool(n1) and "tok/turn" in n1)
n2 = receipt._compaction_nudge(info_for(150000), HOME, once=True)
ck("an immediate repeat at the same context is SILENT (debounced)", n2 == "")
n3 = receipt._compaction_nudge(info_for(160000), HOME, once=True)
ck("a non-material rise (<50%) stays silent", n3 == "")
n4 = receipt._compaction_nudge(info_for(230000), HOME, once=True)
ck("a material rise (>=50% over the last notice) nudges again", bool(n4) and "tok/turn" in n4)

print("-- dropping under the threshold RESETS, so the next crossing nudges once --")
nr = receipt._compaction_nudge(info_for(50000), HOME, once=True)
ck("under threshold is silent", nr == "")
na = receipt._compaction_nudge(info_for(150000), HOME, once=True)
ck("the crossing after a reset nudges once again", bool(na) and "tok/turn" in na)

print("-- the terse nudge NEVER claims a saving (no '~1x cheaper') --")
with open(os.path.join(HOME, "compaction_hint.json"), "w") as f:
    json.dump({"threshold_tokens": 100000, "k": 1.0}, f)   # break-even: the old formatter printed "~1x cheaper"
nb = receipt._compaction_nudge(info_for(300000, session="sess-k1"), HOME, once=True)
ck("a break-even k never renders a 'cheaper' claim", bool(nb) and "cheaper" not in nb)
with open(os.path.join(HOME, "compaction_hint.json"), "w") as f:
    json.dump({"threshold_tokens": 100000, "k": 5.0}, f)   # even a real saving: the one-liner stays claim-free
nc = receipt._compaction_nudge(info_for(300000, session="sess-k5"), HOME, once=True)
ck("even a large k keeps the terse nudge claim-free", bool(nc) and "cheaper" not in nc)

print("-- the passive status line (once=False) is not debounced --")
s1 = receipt._compaction_nudge(info_for(150000, session="sess-B"), HOME, once=False)
s2 = receipt._compaction_nudge(info_for(150000, session="sess-B"), HOME, once=False)
ck("once=False shows the notice on every call while over threshold", bool(s1) and s1 == s2)
ck("the status-line notice is also claim-free", "cheaper" not in s1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_compaction_nudge_debounce: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
