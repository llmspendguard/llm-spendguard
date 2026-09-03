"""calls.context flow-start must NOT re-scan the whole ledger on every enter. Script-style, offline (no LLM).

The bug this guards (2026-09-03): `calls.context().__enter__` called `_flow_start_usd()` → `spent_since('1970-01-01')`
→ a full SCAN of spend_events (356k rows) through the per-row `dec_sum` Decimal aggregate, on EVERY context enter and
un-gated by calls.enabled(). A caller that opens N flow contexts paid N full scans (one honestreview run: ~734 scans,
51 min at 98% CPU). The fix memoizes the whole-ledger countable total on a ledger-freshness token
(SpendLedger.running_llm_total_dec, surfaced as budget.spent_all_time), so repeat flow-starts are O(1).

Invariants:
  • opening K empty flow contexts triggers ZERO full-ledger rescans after the first read (not K) — the regression;
  • the memoized baseline stays CORRECT: it reflects a new charge exactly and equals the live un-memoized sum.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_RECEIPTS"] = "off"   # isolate the flow-ENTER baseline (at 'off', emit_flow returns before the exit read)

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-flowscan-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard
from spendguard import budget
from spendguard.ledger import SpendLedger

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# seed a ledger with real countable spend so there IS a table to (not) scan
for _ in range(50):
    budget.record_charge("openai", "gpt-4o-mini", "realtime", 0.001)

budget.spent_all_time()          # warm the memo once (this legitimately computes the whole-ledger total)

# count ONLY whole-ledger recomputes: running_llm_total_dec calls spent_dec() with since=None; windowed cap/tally
# reads pass since=<date> and are not the thing we're bounding.
_orig_spent_dec = SpendLedger.spent_dec
scan = {"n": 0}
def _counting_spent_dec(self, *a, **kw):
    since = kw.get("since", a[0] if a else None)
    if since is None:
        scan["n"] += 1
    return _orig_spent_dec(self, *a, **kw)
SpendLedger.spent_dec = _counting_spent_dec

K = 25

# A) receipts OFF — the pure enter-baseline path. After warming, K empty contexts must add ZERO rescans.
scan["n"] = 0
for _ in range(K):
    with spendguard.context(intent="probe"):
        pass
ck(f"receipts=off: {K} empty flow contexts → 0 full-ledger rescans (was {K})", scan["n"] == 0)
print(f"    full-ledger recomputes across {K} contexts (off): {scan['n']}")

# B) receipts=flow (the DEFAULT real-world path: enter AND exit both read the baseline). Still O(1), not O(K).
os.environ["SPENDGUARD_RECEIPTS"] = "flow"
scan["n"] = 0
for _ in range(K):
    with spendguard.context(intent="probe"):
        pass
ck(f"receipts=flow: {K} empty flow contexts → O(1) not O(K) rescans", scan["n"] <= 2)
print(f"    full-ledger recomputes across {K} contexts (flow): {scan['n']}")
os.environ["SPENDGUARD_RECEIPTS"] = "off"

# C) correctness — the memo must INVALIDATE on a new write and match the live sum exactly.
b0 = budget.spent_all_time()
budget.record_charge("openai", "gpt-4o-mini", "realtime", 0.25)
b1 = budget.spent_all_time()
ck("memoized baseline reflects a new charge exactly (delta = the charge)", round(b1 - b0, 6) == 0.25)
live = float(budget._ledger().spent_dec())         # the un-memoized whole-ledger total
ck("memoized baseline == live un-memoized whole-ledger sum", round(b1 - live, 9) == 0.0)

SpendLedger.spent_dec = _orig_spent_dec

print(("FAIL: " + ", ".join(fails)) if fails else "all flow-start no-rescan checks passed")
sys.exit(1 if fails else 0)
