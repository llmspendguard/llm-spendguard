"""ItemLedger drives every requested ITEM to a terminal state and reconciles before exit — the item-level
completeness primitive (caller-feedback #6).

Locks the guarantees that make it worth having: answered/refused/unresolved are DISTINCT; the attempt budget and
settled outcomes SURVIVE a restart (the durability the whole thing exists for); reconcile() RAISES while any item is
still pending (the silent-loss it prevents) and passes once every item has a state; reopen() re-pends unresolved items
and that survives a further restart.

Offline, isolated SPENDGUARD_HOME, zero spend (pure file I/O + arithmetic).
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-itemledger-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard.item_ledger import ItemLedger, ANSWERED, REFUSED, UNRESOLVED  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()
ITEMS = ["a", "b", "c", "d", "e"]

# ── a location is REQUIRED (no in-memory-only ledger) ──────────────────────────────────────────────────────────────
try:
    ItemLedger(ITEMS)
    ck("a ledger with no path/job is refused", False, "no error")
except ValueError:
    ck("a ledger with no path/job is refused", True)

L = ItemLedger(ITEMS, job="t1")
ck("the job name resolves to a file under SPENDGUARD_HOME", "item_ledger_t1.jsonl" in L.path and os.environ["SPENDGUARD_HOME"] in L.path)

# ── states + counts + pending ─────────────────────────────────────────────────────────────────────────────────────
L.settle_item("a", ANSWERED, answer="A1")
L.settle_item("b", REFUSED, reason="not a drug")
ck("pending is the untouched items in request order", L.pending() == ["c", "d", "e"], extra=repr(L.pending()))
c = L.state_counts()
ck("counts reflect answered/refused/pending",
   c["answered"] == 1 and c["refused"] == 1 and c["unresolved"] == 0 and c["pending"] == 3 and c["requested"] == 5,
   extra=repr(c))

# ── assert_complete RAISES while anything is pending (the silent-loss guard) ───────────────────────────────────────
try:
    L.assert_complete()
    ck("assert_complete raises while items are pending", False, "did not raise")
except AssertionError:
    ck("assert_complete raises while items are pending", True)

# ── bounded retries → unresolved, DISTINCT from refused ────────────────────────────────────────────────────────────
for _ in range(3):
    L.note_attempt(["c", "d", "e"])
ck("exhausted lists the out-of-budget items", set(L.exhausted(3)) == {"c", "d", "e"}, extra=repr(L.exhausted(3)))
closed = L.close_exhausted(3)
ck("close_exhausted settles all 3 as unresolved", closed == 3)
ck("refused stays DISTINCT from unresolved", L.state["b"] == REFUSED and L.state["c"] == UNRESOLVED)

# ── assert_complete PASSES once every item has a terminal state; the sum holds ──────────────────────────────────────
done = L.assert_complete()
ck("answered + refused + unresolved == requested", done[ANSWERED] + done[REFUSED] + done[UNRESOLVED] == done["requested"])

# ── DURABILITY: a fresh ledger (a restart) recovers outcomes AND the attempt budget ────────────────────────────────
L2 = ItemLedger(ITEMS, job="t1")
ck("restart recovers an answered outcome + its answer", L2.state["a"] == ANSWERED and L2.answers["a"] == "A1")
ck("restart recovers the attempt budget (the retry bound survives)", L2.attempts["c"] >= 3)
ck("restart recovers the unresolved state", L2.state["c"] == UNRESOLVED)

# ── reopen re-pends unresolved items, and that survives a further restart ──────────────────────────────────────────
reopened = L2.reopen()
ck("reopen clears the 3 unresolved items", reopened == 3)
ck("reopened items are pending again", set(L2.pending()) == {"c", "d", "e"}, extra=repr(L2.pending()))
L3 = ItemLedger(ITEMS, job="t1")
ck("a restart AFTER reopen keeps them pending (reopen row clears the stale terminal state)",
   set(L3.pending()) == {"c", "d", "e"}, extra=repr(L3.pending()))

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_item_ledger_completeness: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
