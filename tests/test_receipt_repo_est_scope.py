"""Per-repo est-value scoping — `tally(project=repo)` must return the repo's OWN plan value (est cells whose TEAM or
PROJECT is that repo), not the GLOBAL plan total. Before this, a per-repo receipt line showed its own billed spend
beside the whole plan value, and `_sum_repos` added the global total once PER repo (an N× over-count). Offline: seeds
a deterministic est cache, no LLM, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-repoest-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import receipt, budget                                                 # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
today = receipt._windows()[0]

# Two repos carry plan value on DIFFERENT axes: repo-a as a PROJECT ($10), repo-b as a TEAM ($4). Global = $14.
receipt.stamp_est_value([
    {"day": today, "spend_micros": 10_000_000, "billed": False, "org": "o", "team": "t1", "project": "repo-a"},
    {"day": today, "spend_micros": 4_000_000, "billed": False, "org": "o", "team": "repo-b", "project": "p2"},
], source="claude-code")

print("-- _est_tally: global vs repo-scoped (repo matches on EITHER the team or the project axis) --")
g = receipt._est_tally()
fails += ck("global est = $14 (both repos summed)", g and abs(g["today"] - 14.0) < 1e-9)
a = receipt._est_tally(repo="repo-a")
fails += ck("repo-a scoped = $10 (matched on its PROJECT axis), not the global $14", a and abs(a["today"] - 10.0) < 1e-9)
b = receipt._est_tally(repo="repo-b")
fails += ck("repo-b scoped = $4 (matched on its TEAM axis)", b and abs(b["today"] - 4.0) < 1e-9)
z = receipt._est_tally(repo="nonexistent-repo")
fails += ck("a repo with no est cell = $0 (honest), NOT the global total", z and abs(z["today"] - 0.0) < 1e-9)

print("\n-- tally() wires it: per-repo est is scoped; the global tally stays global --")
budget.spent_since = lambda day, project=None, conv=None: 0.0     # isolate the est axis from the API axis
ta = receipt.tally(project="repo-a")
fails += ck("tally(project='repo-a').est_value is REPO-SCOPED ($10, not $14)",
            (ta.get("est_value") or {}).get("today") == 10.0)
tg = receipt.tally()
fails += ck("tally() global est stays GLOBAL ($14)", (tg.get("est_value") or {}).get("today") == 14.0)

print("\n-- _sum_repos no longer over-counts (the N× bug): sum of repo-scoped est ≈ global, not global×N --")
s = receipt._sum_repos(["repo-a", "repo-b"])
fails += ck("_sum_repos est_month ≈ $14 (repo-scoped sum), NOT $28 (global × 2 repos)", abs(s["est_month"] - 14.0) < 1e-9)

print(f"\n{'[FAIL]' if fails else 'OK'} test_receipt_repo_est_scope: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
