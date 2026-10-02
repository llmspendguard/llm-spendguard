"""budget.by_intent groups billed LLM $ by the forensic intent label, and its Σ RECONCILES EXACTLY with
spent_since for the same window — proved by arithmetic, not by reading the SQL.

by_intent exists so `spendguard receipt --by-intent` answers "what did each job-type cost" (the caller had to query
spend.db by hand). Its one invariant is that it counts the SAME rows the receipt's real-$ API line counts — so a
reader can trust the breakdown sums to the headline. That is locked here: each kind of row is seeded with a DISTINCT
power-of-two amount, so the returned total decomposes to exactly one subset and the arithmetic says which rows were
counted, whatever the SQL looks like. Meta + reconciliation rows must be EXCLUDED (as spent_since excludes them);
a blank-intent row must group under '(unlabelled)', never be dropped.

Offline, isolated SPENDGUARD_HOME, zero spend.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-byintent-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import datetime  # noqa: E402
from spendguard import budget  # noqa: E402

fails = []     # labels of failed checks — a list so ck() mutates it in place, no module-global scalar


def ck(label, cond, extra=""):
    if not cond:
        fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


DAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
# Distinct amounts so any total decomposes to exactly ONE subset of rows. Countable (should appear in by_intent and
# in spent_since): job-a = 1.0 + 2.0 (two calls, one intent → aggregates), job-b = 4.0, unlabelled = 8.0 → Σ = 15.0.
# Excluded (must NOT appear, must NOT change the total): meta = 16.0, reconciled = 32.0.
budget.record_charge("openai", "gpt-5.5", "realtime", 1.0, intent="job-a", basis=budget.BASIS_BILLED)
budget.record_charge("openai", "gpt-5.5", "realtime", 2.0, intent="job-a", basis=budget.BASIS_BILLED)
budget.record_charge("openai", "gpt-5.5", "realtime", 4.0, intent="job-b", basis=budget.BASIS_BILLED)
budget.record_charge("openai", "gpt-5.5", "realtime", 8.0, intent="", basis=budget.BASIS_BILLED)   # blank intent
budget.record_charge("anthropic", "test-model", "meta", 16.0, intent="advisor-meta")              # excluded (is_meta)
budget.record_charge("anthropic", budget._RECONCILED, "batch", 32.0, intent="recon")              # excluded (reconciled)

rows = budget.by_intent(since=DAY)
sigma = sum(v["cost"] for v in rows.values())
spent = float(budget.spent_since(DAY))

# ── the invariant: Σ over intents == the receipt's API line for the window, exactly ──────────────────────────────
ck("Σ by_intent == spent_since (reconciles with the API line)", abs(sigma - spent) < 1e-9,
   extra=f"Σ={sigma!r} spent_since={spent!r}")
ck("the countable total is exactly 1+2+4+8 = 15.0 (no excluded row leaked in)", abs(sigma - 15.0) < 1e-9,
   extra=f"Σ={sigma!r}")

# ── grouping: same-intent rows aggregate; cost and call count are both right ─────────────────────────────────────
ck("job-a aggregates its two calls to $3.00", abs(rows.get("job-a", {}).get("cost", 0) - 3.0) < 1e-9,
   extra=repr(rows.get("job-a")))
ck("job-a reports 2 calls", rows.get("job-a", {}).get("calls") == 2, extra=repr(rows.get("job-a")))
ck("job-b is $4.00 / 1 call", rows.get("job-b", {}).get("cost") == 4.0 and rows.get("job-b", {}).get("calls") == 1,
   extra=repr(rows.get("job-b")))

# ── blank intent is surfaced, never dropped ──────────────────────────────────────────────────────────────────────
ck("a blank-intent row groups under '(unlabelled)' at $8.00", abs(rows.get("(unlabelled)", {}).get("cost", 0) - 8.0) < 1e-9,
   extra=repr(rows.get("(unlabelled)")))

# ── exclusions: meta + reconciliation rows are absent (their intent labels never appear) ─────────────────────────
ck("the meta row's intent is excluded", "advisor-meta" not in rows, extra=f"keys={sorted(rows)}")
ck("the reconciliation row's intent is excluded", "recon" not in rows, extra=f"keys={sorted(rows)}")

print(f"\n{'OK' if not fails else 'FAIL'} test_by_intent_reconciles_with_spent_since: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
