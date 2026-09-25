"""Routing provenance in the ledger (Ash 2026-09-25: "record the request, what was actually called, and WHY so we can
figure out to improve"). The decisions table records requested_model → chosen_model; this adds a `why` column carrying
the SPECIFIC routing reason (bandit → codex, tier-3 base, best-value, …), so "a cheap intent ran on an expensive model
because <X>" is a queryable fact — the exact Warden #1 failover. Guards: the column + migration exist; record_decision
stores `why`; _book_substitution passes the substitution's specific reason (not just the coarse basis)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-why-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import guard, adapters  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


db = guard._decisions_db()
cols = [c[1] for c in db.execute("PRAGMA table_info(decisions)").fetchall()]
ck("decisions has a `why` column (request/actual/WHY)", "why" in cols)

print("-- record_decision stores the specific why --")
guard.record_decision(intent="warden:describe", requested_model="openai:gpt-5.4-nano",
                      chosen_model="openai:gpt-5.6-sol", counterfactual_usd=0.0, actual_usd=0.9,
                      saved_usd=0.0, basis="advisor", why="bandit → codex (gpt-5.6-sol)")
row = db.execute("SELECT requested_model, chosen_model, why FROM decisions WHERE intent='warden:describe' "
                 "ORDER BY rowid DESC LIMIT 1").fetchone()
ck("requested → chosen recorded", row[0] == "openai:gpt-5.4-nano" and row[1] == "openai:gpt-5.6-sol")
ck("the specific WHY is recorded (queryable failover reason)", row[2] == "bandit → codex (gpt-5.6-sol)")

print("-- _book_substitution passes the substitution's SPECIFIC reason as why (not only the coarse basis) --")
# a substituted result: nano requested, sol served, with a specific substitution reason
r = {"provider": "openai", "model": "gpt-5.6-sol", "substituted_from": "openai:gpt-5.4-nano",
     "substitution": "lane-balance: bandit → codex (gpt-5.6-sol)", "cost": 0.9, "in_tok": 100, "out_tok": 5000,
     "best_value": False, "requested_effort": None, "chosen_effort": None}
adapters._book_substitution(r)
row2 = db.execute("SELECT basis, why, requested_model, chosen_model FROM decisions "
                  "WHERE why LIKE 'lane-balance:%' ORDER BY rowid DESC LIMIT 1").fetchone()
ck("_book_substitution booked the row", row2 is not None)
if row2:
    ck("basis stays coarse (keys the savings tally)", row2[0] in ("advisor", "best-value"))
    ck("why carries the SPECIFIC reason", row2[1] == "lane-balance: bandit → codex (gpt-5.6-sol)")
    ck("requested=nano, chosen=sol (the failover is visible)",
       row2[2] == "openai:gpt-5.4-nano" and row2[3] == "openai:gpt-5.6-sol")

print(f"\n{'[FAIL]' if _fails else 'OK'} test_decision_why_provenance: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
