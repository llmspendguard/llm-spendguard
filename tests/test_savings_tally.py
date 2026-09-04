"""Savings tally — the running "$ spendguard saved" as an HONEST third axis. Script-style, offline (no LLM, no net).

Guards the five pieces wired 2026-09-03:
  1. advisor  — a metered substitution to a CHEAPER model is credited (requested = baseline); a $0 plan substitution
     is NOT (that avoided-API value is the est-value axis — booking it here would double-count);
  2. compaction — a compaction event credits the avoided context re-read $ (conservative one-turn floor);
  3. saved_since — the tally splits CERTAIN (measured) from COUNTERFACTUAL and never blurs them;
  4. double-count guard — record_saving REFUSES the reserved 'plan' source (that IS est-value);
  5. savings_sanity — a saving many times the baseline is FLAGGED, not hidden; baseline 0 = unjudgeable, not flagged.
Plus a wiring check: the credit helpers are actually CALLED from the public paths (adapters.call / record_sessionstart).
"""
import os
import sys
import tempfile
import inspect

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-savings-")
    _self = os.path.realpath(__file__)                    # contain the spawn: re-exec THIS file, validated under tests/
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import guard, compaction, adapters, pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _saved(source):
    return round(sum(r["k1"] for r in guard.by_dims_guarded() if r["source"] == source), 6)


# ── item 4: record_saving REFUSES the reserved 'plan' source (double-counts the est-value axis) ──
guard.record_saving("plan", 5.0)
ck("item4: record_saving('plan') refused — no plan row booked (est-value, not the savings ledger)", _saved("plan") == 0.0)
guard.record_saving("cache", 2.0)                        # a normal (measured) source still records
ck("a normal source still records", _saved("cache") == 2.0)

# ── item 2: a compaction event credits the avoided re-read $ (dropped tokens × cached-in rate) ──
_orig_price = pricing.price
pricing.price = lambda m: {"cached_in": 1.0}             # $1.00 / 1M cached-input tokens
compaction._credit_compaction_saving({"model": "m", "pre_context": 1_000_000}, 200_000)   # 800k dropped × $1/1M = $0.80
ck("item2: compaction credited the avoided re-read $ (0.80)", _saved("compaction") == 0.80)
pricing.price = _orig_price
ck("item2 wiring: record_sessionstart calls the compaction credit",
   "_credit_compaction_saving" in inspect.getsource(compaction.record_sessionstart))

# ── item 1: a metered substitution to a CHEAPER model is credited 'advisor'; a $0 plan substitution is NOT ──
_orig_rt = pricing.realtime_cost
pricing.realtime_cost = lambda model, i, o: 0.10 if model == "expensive:m" else 0.02   # baseline (requested) = $0.10
adapters._maybe_credit_advisor("expensive:m", {"substituted_from": "expensive:m", "cost": 0.02, "in_tok": 100, "out_tok": 50})
ck("item1: metered cheaper substitution credited 'advisor' (0.10 baseline − 0.02 actual = 0.08)", _saved("advisor") == 0.08)
_before = _saved("advisor")
adapters._maybe_credit_advisor("expensive:m", {"substituted_from": "expensive:m", "cost": 0.0, "in_tok": 100, "out_tok": 50})
ck("item1: a $0 plan substitution is NOT booked as advisor (that is the est-value axis)", _saved("advisor") == _before)
pricing.realtime_cost = _orig_rt
ck("item1 wiring: adapters.call calls the advisor credit", "_maybe_credit_advisor" in inspect.getsource(adapters.call))

# ── item 3: saved_since splits CERTAIN (measured) from COUNTERFACTUAL, total = sum, never blurred ──
s = guard.saved_since()
ck("item3: cache counts as CERTAIN; advisor+compaction as COUNTERFACTUAL",
   s["by_source"].get("cache") == 2.0 and s["certain"] == 2.0 and round(s["counterfactual"], 6) == 0.88)
ck("item3: total = certain + counterfactual (a 3rd axis, not summed into real-$/est-value)",
   round(s["total"], 6) == round(s["certain"] + s["counterfactual"], 6) == 2.88)

# ── item 5: cross-check is TRANSPARENT — facts + decomposition, NO hand-picked plausibility verdict ──
cx = guard.savings_crosscheck(1000.0)
ck("item5: cross-check returns the ratio as a FACT (saved/baseline), and NO threshold verdict",
   cx["ratio"] == round(cx["saved"] / 1000.0, 2) and "plausible" not in cx)
ck("item5: the total is AUDITABLE — Σ(by_source) == saved (a bogus source is visible, not hidden in a total)",
   round(sum(cx["by_source"].values()), 6) == round(cx["saved"], 6) == 2.88)
ck("item5: baseline 0 → ratio None (unjudgeable), still no verdict", guard.savings_crosscheck(0.0)["ratio"] is None)

print(("[OK]" if not fails else "[FAIL]") + " savings tally: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
