"""Every requested ITEM ends in a NAMED state, or the run fails loudly — the item-level completeness primitive.

THE FAILURE THIS EXISTS TO PREVENT
----------------------------------
spendguard's resume machinery (bulk_delegate's content-addressed checkpoint, the Batch-API offload) is keyed to the
unit it SEES — a task, a chunk, a batch. A batch that treats the BATCH as its unit of work loses ITEMS silently: a
reply that answers 28 of 30 packed items still parses, so the batch is recorded done and its resume key marked
complete — and the 2 unanswered items are gone. Nothing errored, nothing was logged with a name, and the aggregate
counter said "2 missing from reply" while the run reported success.

Measured by a caller: 753 medications were left with a GPI class and no subclass that way. They sat inside batches
that SUCCEEDED, so even the resume would not retry them — their keys were recorded as done. A per-batch checkpoint
cannot catch this; the unit of accounting has to be the ITEM.

THE RULE
--------
The unit of work is the ITEM. Every requested item ends in exactly one terminal state and the run reconciles (via
assert_complete) before it exits:

    answered    a usable answer came back
    refused     the model explicitly said none of the options applies — a real answer, not a gap, kept DISTINCT
    unresolved  it was asked for and never answered, after bounded retries — recorded WITH its reason + attempt count

    answered + refused + unresolved == requested      (asserted, not hoped)

An item missing from a reply is neither an error nor a success: it returns to the pending pool and is re-batched.
Only after RETRY_ROUNDS attempts does it become `unresolved` — a named, countable, re-runnable gap, not an absence.

DURABILITY (why this module exists at all)
------------------------------------------
Every outcome and every attempt is APPEND-ONLY to a JSONL under SPENDGUARD_HOME (`_append_row`, flushed before the
next call), and the run asserts completeness at exit (`assert_complete`). So loss is LOUD, not silent: a crash, a
spend cap, or a restart recovers both the settled outcomes and the retry budget (`_recover_state`), and an item that
never reached a batch is caught by the exit assertion rather than vanishing. This IS the durable, loss-loud path —
there is no single-copy in-memory state that a crash could drop.

WHY REFUSED IS NOT UNRESOLVED
-----------------------------
"None of these applies" is the correct answer for a concept that is not a drug, and forcing it into a category would
be worse than leaving it out. Collapsing the two would either hide real gaps among legitimate refusals or inflate the
gap count with items answered correctly — so the ranker / downstream never learns the true answer rate.

WHY ATTEMPTS ARE PERSISTED, NOT JUST COUNTED
--------------------------------------------
The attempt count is written for PENDING items too, not only when an item settles. Holding it in memory alone means a
restart resets every pending item's budget to zero, so an item the model will never answer is retried forever across
restarts — the retry bound would exist in one process and nowhere else, and the cost of a permanently-unanswerable
item would be unbounded in practice.

Use it ALONGSIDE the batch gate (bulkgate.gated_batch) / the realtime fan (lane_balance.bulk_delegate): the gate
governs SPEND per call-class; this ledger governs COMPLETENESS per item. They are orthogonal and both are needed.
"""
from __future__ import annotations

import collections
import json
import os
import time

from . import config

ANSWERED, REFUSED, UNRESOLVED = "answered", "refused", "unresolved"

# How many times an item may be re-batched before it is called unresolved. Three covers the common case (a model
# omitting an item from one crowded reply) while bounding the cost of an item that will never come back. Override per
# call via close_exhausted(rounds=...) / exhausted(rounds=...), or globally via config `bulkgate.item_retry_rounds`.
RETRY_ROUNDS = 3


def _default_rounds() -> int:
    try:
        return max(1, int(config._cfg_get("bulkgate", "item_retry_rounds", RETRY_ROUNDS)))
    except (TypeError, ValueError):
        return RETRY_ROUNDS


class ItemLedger:
    """Tracks every requested item to a terminal state, persisting as it goes (append-only, loss-loud).

    The backing file is an append-only JSONL holding one row per settled item and one row per attempt round, so
    neither answers nor retry budgets are lost to a crash, a spend cap or a restart. Items are identified by a
    caller-supplied id that must be STABLE across runs (the same id must name the same item next run, or resume
    re-does work / loses track).

    Location: pass an explicit `path`, OR a `job` name and the file lands at SPENDGUARD_HOME/item_ledger_<job>.jsonl
    (the same HOME every other spendguard adapter persists under). One of the two is required — an item ledger with no
    durable file cannot survive the restart it exists to survive.
    """

    def __init__(self, requested, *, path: str = None, job: str = None):
        if not path and not job:
            raise ValueError("ItemLedger needs a durable location: pass path=<jsonl> or job=<name> "
                             "(→ SPENDGUARD_HOME/item_ledger_<job>.jsonl). An in-memory-only ledger cannot survive a "
                             "restart, which is the whole point.")
        self.path = str(path) if path else str(config.HOME / f"item_ledger_{job}.jsonl")
        self.requested = list(dict.fromkeys(requested))   # de-duped, order kept
        self.state: dict = {}
        self.answers: dict = {}
        self.reasons: dict = {}
        self.attempts: collections.Counter = collections.Counter()
        self._recover_state()

    def _recover_state(self) -> None:
        """Recover settled outcomes AND attempt counts from a previous run's append-only file."""
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue                              # a torn final line from a crash is skipped, not fatal
                item = row.get("item")
                if item is None:
                    continue
                if row.get("attempts") is not None:
                    # Attempts only ever grow, so the highest seen is the truth whatever order the rows were written in.
                    self.attempts[item] = max(self.attempts[item], int(row["attempts"]))
                if row.get("reopened"):
                    self.state.pop(item, None)            # a reopen row clears a prior terminal state (re-run of unresolved)
                    continue
                if row.get("state") in (ANSWERED, REFUSED, UNRESOLVED):
                    self.state[item] = row["state"]
                    if "answer" in row:
                        self.answers[item] = row["answer"]
                    if row.get("reason"):
                        self.reasons[item] = row["reason"]

    def pending(self) -> list:
        """Requested items with no terminal state yet, in request order.

        `unresolved` is terminal but RETRYABLE on a later run: a fresh run calls `reopen()` first if it wants to try
        them again, so an item parked by a bounded retry budget is not parked forever by accident.
        """
        return [i for i in self.requested if i not in self.state]

    def reopen(self) -> int:
        """Clear `unresolved` items and their attempt budget for a fresh attempt. Returns how many were reopened."""
        reopened = [i for i, s in self.state.items() if s == UNRESOLVED]
        for item in reopened:
            del self.state[item]
            self.attempts[item] = 0
            self._append_row({"item": item, "attempts": 0, "reopened": True})
        return len(reopened)

    def _append_row(self, row: dict) -> None:
        """Append one row to the durable JSONL and flush — the single write path, so every outcome/attempt is on disk
        before the next call runs (a crash loses nothing already settled)."""
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({**row, "at": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
            handle.flush()

    def settle_item(self, item, state: str, answer=None, reason: str = "") -> None:
        """Settle one item to a terminal state. Appended to the durable file immediately, so nothing is lost on a crash."""
        if state not in (ANSWERED, REFUSED, UNRESOLVED):
            raise ValueError(f"unknown terminal state {state!r} — one of {ANSWERED!r}/{REFUSED!r}/{UNRESOLVED!r}")
        self.state[item] = state
        if answer is not None:
            self.answers[item] = answer
        if reason:
            self.reasons[item] = reason
        self._append_row({"item": item, "state": state, "answer": answer, "reason": reason or None,
                          "attempts": int(self.attempts[item])})

    def note_attempt(self, items) -> None:
        """Count and PERSIST one attempt against each item actually asked about.

        Written to disk, not just incremented in memory: an in-memory-only count means a restart hands every pending
        item a fresh retry budget, so the bound would exist inside one process and nowhere else.
        """
        for item in items:
            self.attempts[item] += 1
            self._append_row({"item": item, "attempts": int(self.attempts[item])})

    def exhausted(self, rounds: int = None) -> list:
        """Pending items already asked `rounds` times — out of retry budget. `rounds` defaults to the configured bound."""
        rounds = _default_rounds() if rounds is None else rounds
        return [i for i in self.pending() if self.attempts[i] >= rounds]

    def close_exhausted(self, rounds: int = None) -> int:
        """Settle out-of-budget items as `unresolved`, with their attempt count. Returns how many were closed.

        A named gap, not an absence: the item appears in the output with the number of times it was asked, so "the
        model never answered this" is visible and re-runnable rather than looking like it was never requested.
        """
        stale = self.exhausted(rounds)
        for item in stale:
            self.settle_item(item, UNRESOLVED, reason=f"no answer after {self.attempts[item]} attempt(s)")
        return len(stale)

    def state_counts(self) -> dict:
        tally = collections.Counter(self.state.values())
        return {"requested": len(self.requested), ANSWERED: tally[ANSWERED], REFUSED: tally[REFUSED],
                UNRESOLVED: tally[UNRESOLVED], "pending": len(self.pending())}

    def assert_complete(self) -> dict:
        """Assert every requested item is accounted for (the reconciliation gate). Raises AssertionError if not.

        This is the gate that makes the structure worth having. Without it the ledger merely records what happened to
        the items it heard about, and an item dropped before it reached a batch — by a filter, a bad id, a slicing bug
        — would still vanish without trace. (Named assert_complete, not 'reconcile', so it is never confused with the
        PROVIDER-TRUTH reconciliation in reconcile.py / reconcile_calls.py — a different job.)
        """
        counts = self.state_counts()
        settled = counts[ANSWERED] + counts[REFUSED] + counts[UNRESOLVED]
        if settled + counts["pending"] != counts["requested"]:
            raise AssertionError(
                f"item ledger does not reconcile: {settled} settled + {counts['pending']} pending != "
                f"{counts['requested']} requested. Items were lost between the request and the ledger.")
        if counts["pending"]:
            raise AssertionError(
                f"{counts['pending']} item(s) are still PENDING at the end of the run — requested, never answered, "
                f"never settled as unresolved. That is the silent loss this ledger exists to prevent; close them "
                f"(close_exhausted) before exiting.")
        return counts

    def print_accounting(self) -> dict:
        """Print the final accounting and assert completeness. Returns the counts."""
        counts = self.assert_complete()
        print("\n  ITEM ACCOUNTING  (every requested item has a state)")
        print(f"    requested  {counts['requested']:,}")
        print(f"    answered   {counts[ANSWERED]:,}")
        print(f"    refused    {counts[REFUSED]:,}   (model said none applies — an answer, not a gap)")
        print(f"    unresolved {counts[UNRESOLVED]:,}   (asked, never answered — named and re-runnable)")
        if counts[UNRESOLVED]:
            for item in [i for i, s in self.state.items() if s == UNRESOLVED][:6]:
                print(f"       {item!r}: {self.reasons.get(item, '')}")
        print(f"    reconciled: {counts[ANSWERED]} + {counts[REFUSED]} + {counts[UNRESOLVED]} == "
              f"{counts['requested']} ✓")
        return counts
