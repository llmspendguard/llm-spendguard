"""Which rows each cost aggregator counts — proved by ARITHMETIC, not by reading the SQL.

WHY. `charges` carries four kinds of row that are not ordinary workload spend:

    quarantined     an estimate the gate proved impossible (kept for forensics, never spend)
    reconciled      provider-truth backfill / reconstruction — MIRRORS another source
    true-down       negative corrections netting an estimate down to billed actuals
    meta            spendguard's own advisor spend, capped separately

Every reader must make a deliberate choice about each, and the RIGHT ANSWER DIFFERS: the SaaS push payload
must include reconciled rows; `gate_batch_cells` must exclude true-down because it is the INPUT to true-down
and netting twice would double-count. A blanket rule would be wrong. What is not allowed is an unconsidered
answer.

This bit twice. Quarantine was added to `spent_since` and `by_provider_day` and missed on `by_day` — which the
leak check reads, so the leak view kept counting an invented $54.51 — and on `by_dims`, the push payload,
which would have shipped it to the org dashboard. Both misses were "I fixed the readers I could think of".

HOW THIS TESTS IT. Not by grepping for a marker name — a query can mention one in a comment and not use it,
or exclude rows by some other mechanism, and neither would be visible to a scan. Instead each kind of row is
seeded with a DISTINCT power-of-two amount, so the total returned by any aggregator decomposes to exactly one
subset: the arithmetic says which rows it counted, whatever the SQL looks like.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-matrix-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import re, inspect, pathlib, itertools, datetime
from spendguard import budget

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


DAY = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
# Distinct powers of two → any total decomposes to exactly ONE subset, so the sum itself reveals which rows
# an aggregator counted. True-down is negative because that is what a correction row really is.
AMOUNTS = {"plain": 1.0, "quarantined": 2.0, "reconciled": 4.0, "true_down": -8.0, "meta": 16.0}


def seed():
    budget.record("anthropic", "test-model", "batch", AMOUNTS["plain"], project="p1")
    budget.record("anthropic", "test-model", "batch", AMOUNTS["quarantined"], project="p1")
    # Target by ROWID. Seeding these in the same second is exactly the collision that made a ts-targeted
    # quarantine tag the plain row too — the failure this test found in the repair tool itself.
    rid = budget._db().execute("SELECT rowid FROM charges WHERE cost=?", (AMOUNTS["quarantined"],)).fetchone()[0]
    budget.quarantine_charge(reason="seed: impossible estimate", row=rid)
    budget.record("anthropic", budget._RECONCILED, "batch", AMOUNTS["reconciled"], project="p1")
    budget.record("anthropic", "test-model", "batch", AMOUNTS["true_down"], project="p1",
                  conv_id=budget._TRUE_DOWN_CONV)
    budget.record("anthropic", "test-model", "meta", AMOUNTS["meta"], project="p1")


def _total(res):
    """Sum the money out of whatever shape an aggregator returns (float · {k: $} · {k: {'cost': $}} · [{...}])."""
    if isinstance(res, (int, float)):
        return float(res)
    if isinstance(res, dict):
        vals = list(res.values())
        if vals and isinstance(vals[0], dict):
            return sum(float(v.get("cost", 0)) for v in vals)
        return sum(float(v) for v in vals)
    if isinstance(res, list):
        return sum(float(r.get("cost", 0)) for r in res if isinstance(r, dict))
    return 0.0


def decompose(total):
    """Which seeded rows produced this total. Unique by construction; None if it cannot be explained (which is
    itself a finding — it means the aggregator returned money we did not put there)."""
    names = list(AMOUNTS)
    for r in range(len(names) + 1):
        for combo in itertools.combinations(names, r):
            if abs(sum(AMOUNTS[c] for c in combo) - total) < 1e-6:
                return set(combo)
    return None


seed()

# aggregator → the rows it SHOULD count, and why it differs from its neighbours.
MATRIX = {
    "spent_since":           ({"plain", "true_down"},   "headline workload; corrections net against estimates"),
    "quarantined_since":     ({"quarantined"},          "it is ABOUT quarantined rows — it selects them"),
    "by_provider_day":       ({"plain", "true_down"},   "compared to provider truth: like-for-like (called with kind='batch')"),
    "reconciled_by_project": ({"reconciled"},           "selects the backfill rows by marker model"),
    "gate_by_project_day":   ({"plain", "true_down"},   "the gate-attributed side of the reconcile loop (kind='batch')"),
    "gate_batch_cells":      ({"plain"},                "INPUT to true_down — counting an existing correction would net twice"),
    "meta_spent_since":      ({"meta"},                 "spendguard's own spend, capped separately"),
    # by_day's default is "everything the ledger accounts for", backfill INCLUDED — the leak check depends on
    # that (excluding reconciled rows would re-flag a gap a previous reconcile already absorbed). Quarantine is
    # the one thing it must never count, which is exactly the miss that kept the leak view wrong.
    "by_day":                ({"plain", "true_down", "meta", "reconciled"},
                              "what the LEAK CHECK reads — accounted = gate-recorded + backfill"),
    "by_dims":               ({"plain", "reconciled", "true_down", "meta"}, "the SaaS PUSH payload — the org needs backfill, never quarantine"),
    "by_key":                ({"plain"},                "per-key workload spend"),
}
# Called the way PRODUCTION calls them — a matrix that only holds for argument-less calls would prove nothing
# about the paths that actually run.
CALLS = {"spent_since": ("2000-01-01",), "quarantined_since": ("2000-01-01",), "meta_spent_since": ("2000-01-01",)}
KWARGS = {"by_provider_day": {"kind": "batch"}, "gate_by_project_day": {"kind": "batch"}}


def aggregators():
    """Every budget.py function that SUMS money from `charges`. DISCOVERED, not listed — a new aggregator must
    appear here whether or not anyone remembered to mention it. (Finding the functions is format-determined;
    what they COUNT is settled by arithmetic below, never by reading the SQL.)"""
    src = pathlib.Path(inspect.getfile(budget)).read_text()
    return {blk.split("(")[0].strip(): blk for blk in re.split(r"\ndef ", src)
            if "SUM(cost)" in blk and "FROM charges" in blk}


found = aggregators()
print("-- every money aggregator is accounted for in the reviewed matrix --")
check("the scan found the aggregators", len(found) >= 8, str(len(found)))
missing = sorted(set(found) - set(MATRIX))
check("no aggregator is missing from the matrix (add new ones deliberately)", not missing, f"unreviewed: {missing}")
gone = sorted(set(MATRIX) - set(found))
check("no matrix entry names a function that no longer sums money", not gone, f"stale: {gone}")

print("-- what each one COUNTS, proved by decomposing its total --")
for name, (want, why) in sorted(MATRIX.items()):
    fn = getattr(budget, name, None)
    if fn is None:
        check(f"{name} exists", False)
        continue
    try:
        got = decompose(_total(fn(*CALLS.get(name, ()), **KWARGS.get(name, {}))))
    except Exception as e:
        check(f"{name}() executes", False, f"{type(e).__name__}: {e}")
        continue
    check(f"{name} counts {sorted(want)} — {why}", got == want, f"actually counted {sorted(got) if got else got}")

print("-- the repair tool cannot tag rows it was not aimed at --")
# Found by this very test: charges.ts has SECOND granularity, and seeding two rows in one second made a
# ts-targeted quarantine tag both. Six charges share a second in the real ledger.
shared_ts = budget._db().execute("SELECT ts FROM charges GROUP BY ts HAVING COUNT(*) > 1 LIMIT 1").fetchone()
if shared_ts:
    try:
        budget.quarantine_charge(ts=shared_ts[0], reason="should refuse")
        refused = False
    except ValueError as e:
        refused, why = True, str(e)
    check("an ambiguous timestamp is REFUSED, not applied to every row", refused)
    check("…and the message lists the candidates and how to disambiguate",
          refused and "--row" in why and "row " in why)
else:
    check("(no shared timestamp in this fixture to test ambiguity with)", True)

print("-- the invariant behind the whole matrix --")
q_counted = [n for n, (w, _) in MATRIX.items() if "quarantined" in w and n != "quarantined_since"]
check("NOTHING except quarantined_since counts a quarantined row", not q_counted, str(q_counted))
check("a quarantined row is still IN the table (excluded ≠ deleted)",
      budget._db().execute("SELECT COUNT(*) FROM charges WHERE cost=?",
                           (AMOUNTS["quarantined"],)).fetchone()[0] == 1)

print(f"\n{'[FAIL]' if failures else 'OK'} test_ledger_marker_matrix: {failures} failure(s)")
sys.exit(1 if failures else 0)
