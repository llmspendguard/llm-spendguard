"""config.update_json BOUNDS the `<file>.corrupt.<stamp>` quarantine — the fix for the measured leak: 142
`resource_state_state.json.corrupt.*` copies had accumulated unpruned (2026-10-09), each a rebuildable cache moved
aside on a parse failure and never cleaned. After a quarantine, only the most recent N per BASE file are kept
(safety.corrupt_keep, default 3; env SPENDGUARD_CORRUPT_KEEP; 0 = keep all). Guards: a parse failure quarantines +
rebuilds (nothing destroyed — the data is in a .corrupt copy); the copies are bounded to N; the bound is PER base
file (one cache's churn never prunes another's); env override wins; the rebuilt file parses. Offline, isolated HOME."""
import os
import sys
import json
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-corrupt-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_CORRUPT_KEEP"] = "3"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

HOME = config.HOME
A = HOME / "rebuildable_cache_a.json"
B = HOME / "rebuildable_cache_b.json"
def _corrupts(p):
    return sorted(p.parent.glob(p.name + ".corrupt.*"))


# Pre-seed 7 OLD quarantine copies with distinct stamps (the real leak accumulated over a month, not one second —
# the stamp is per-second, so same-second re-corruptions legitimately overwrite one name). Then a fresh parse
# failure quarantines an 8th and the bound must prune to the newest 3.
for yr in range(2019, 2026):
    (HOME / f"{A.name}.corrupt.{yr}0101T000000Z").write_text("old corrupt from %d" % yr)
ck("pre-seeded 7 old corrupt copies", len(_corrupts(A)) == 7)

A.write_text("}{ not json — a fresh corruption")
out = config.update_json(A, lambda d: {"rebuilt": True}, quarantine_unparseable=True, reason="test")
ck("the rebuilt file parses (quarantine recovered, not wedged)", out is not None and json.loads(A.read_text()).get("rebuilt") is True)
ck("corrupt copies are BOUNDED to safety.corrupt_keep=3 (8 existed → 3 kept)", len(_corrupts(A)) == 3)
ck("the 3 kept are the MOST RECENT (the fresh one + the two newest old; stamp sorts lexically)",
   _corrupts(A) == sorted(_corrupts(A))[-3:] and any("2025" in p.name or ".corrupt.2026" in p.name for p in _corrupts(A)))

# ── the bound is PER base file: churning A must not prune B's quarantine copies ──
(HOME / f"{B.name}.corrupt.20200101T000000Z").write_text("b old")
B.write_text("garbage")
config.update_json(B, lambda d: {"b": 1}, quarantine_unparseable=True, reason="test")
ck("a different base file keeps its own copies (scoped per base name — A's churn never pruned B)", len(_corrupts(B)) == 2)
ck("...and A still has exactly its own 3 (B's churn did not touch them)", len(_corrupts(A)) == 3)

# ── env override wins (so a corrupt config.json can never wedge the bound); 0 = keep all ──
os.environ["SPENDGUARD_CORRUPT_KEEP"] = "1"
A.write_text("still bad")
config.update_json(A, lambda d: {"rebuilt": "x"}, quarantine_unparseable=True, reason="test")
ck("env SPENDGUARD_CORRUPT_KEEP=1 is honored (prunes down to 1)", len(_corrupts(A)) == 1)
ck("keep_corrupt_default reads the env override", config.keep_corrupt_default() == 1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_corrupt_quarantine_bound: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
