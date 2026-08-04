"""The impossible-estimate rail — money that never happened must not enter the ledger looking like money.

WHAT WENT WRONG (real ledger, 2026-08-04): the base64-as-tokens bug recorded a batch at 48,110,544 input
tokens — 10 requests, so 4.8M each against claude-sonnet-5's 1,000,000 context window — and NOTHING objected.
It became a $54.51 charge attributed to a real project. `reconcile-ledger` stayed quiet too, because its leak
check only ever looked for money that was MISSING, never for money that was INVENTED.

The rail is a physical bound, not a tuned threshold: a request larger than the model's published context
window is rejected by the provider, so an estimate implying one describes a broken estimator, not a batch.
When the limit is unknown we say NOTHING rather than invent a bound.

Invariants pinned here:
  • the rail fires on the real numbers and stays silent on the real good ones;
  • silence when the model's limit is unknown — no invented bounds;
  • a quarantined row is EXCLUDED from every total but never deleted (forensics: what was claimed, and when);
  • provider-truth comparison excludes it too, so reconcile compares like with like;
  • the receipt SHOWS the exclusion — a number that quietly vanishes is its own dishonesty.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-plaus-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import io, contextlib, json, pathlib
from spendguard import gate, budget, pricing, receipt, config

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


# The rail reads limits from the synced price cache; seed one so the test never depends on a network fetch.
MODEL, LIMIT = "test-model-1m", 1_000_000
pathlib.Path(config.HOME).mkdir(parents=True, exist_ok=True)
pathlib.Path(config.HOME, "litellm_prices.json").write_text(json.dumps(
    {"models": {}, "providers": {}, "unit_models": {},
     "context": {MODEL: {"max_input_tokens": LIMIT, "max_output_tokens": 128_000}}}))
pricing.CONTEXT_LIMITS = pricing._load_context()

print("-- the limits ride along with the prices (same upstream file), and are read back --")
check("max_input_tokens resolves from the synced cache", pricing.max_input_tokens(MODEL) == LIMIT)
check("an unknown model returns None, not a default", pricing.max_input_tokens("no-such-model") is None)
from spendguard import sync
import inspect
check("sync passes the context fields through to the cache",
      "max_input_tokens" in inspect.getsource(sync) and '"context"' in inspect.getsource(sync))

print("-- the rail, on the REAL numbers from the corrupted row --")
bad, facts = gate._implausible_estimate(MODEL, 48_110_544, 10)       # 4.8M per request vs a 1M window
check("fires on 48,110,544 over 10 requests", bad is True)
# The rail reports FACTS, so the test asserts values — not whether a sentence is worded persuasively.
check("it reports the per-request figure that proves it", round(facts["per_req"]) == 4_811_054,
      f"{facts.get('per_req')}")
check("…against the limit it compared to", facts["limit"] == LIMIT and facts["requests"] == 10)
check("silent on the same batch's plausible sibling (8,175 over 10)",
      gate._implausible_estimate(MODEL, 8_175, 10)[0] is False)
check("silent at exactly the limit (the bound is inclusive, not off-by-one)",
      gate._implausible_estimate(MODEL, LIMIT * 4, 4)[0] is False)
check("fires one token past it", gate._implausible_estimate(MODEL, LIMIT * 4 + 4, 4)[0] is True)
print("-- no invented bounds --")
check("unknown model → no opinion", gate._implausible_estimate("no-such-model", 10**9, 1)[0] is False)
check("unknown request count → no opinion", gate._implausible_estimate(MODEL, 10**9, 0)[0] is False)
check("zero tokens → no opinion", gate._implausible_estimate(MODEL, 0, 10)[0] is False)

print("-- it SHOUTS, once, not once per request --")
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    for _ in range(50):
        gate._warn_implausible(MODEL, 48_110_544, 10)
out = buf.getvalue()
check("something is written to stderr", out.strip() != "")
check("exactly one emission for 50 identical calls (dedup, not silence)",
      out.count("IMPOSSIBLE ESTIMATE") == 1, f"{out.count('IMPOSSIBLE ESTIMATE')}×")
check("the warn path still returns the facts to its caller",
      gate._warn_implausible(MODEL, 48_110_544, 10).get("limit") == LIMIT)

print("-- quarantined money is excluded from every total, and never deleted --")
DAY = budget._db().execute("SELECT date('now')").fetchone()[0]
budget.record("anthropic", MODEL, "batch", 54.51, project="saga")
plain = budget.spent_since(DAY)
check("a normal charge counts", plain >= 54.51, f"{plain}")
ts = budget._db().execute("SELECT ts FROM charges WHERE cost=54.51").fetchone()[0]
n = budget.quarantine_charge(ts, "test: base64-as-tokens")
check("exactly one row is tagged", n == 1, f"{n}")
check("re-running is a no-op (idempotent)", budget.quarantine_charge(ts, "again") == 0)
check("it drops out of the spend total", abs(budget.spent_since(DAY) - (plain - 54.51)) < 0.005,
      f"{budget.spent_since(DAY)} vs {plain - 54.51}")
row = budget._db().execute("SELECT cost, model, conv_id FROM charges WHERE ts=?", (ts,)).fetchone()
check("the ROW still exists with its amount intact (forensics, not deletion)",
      row is not None and abs(row[0] - 54.51) < 1e-9 and row[1] == MODEL)
check("…tagged as quarantined", row[2] == budget.QUARANTINE_CONV)
check("provider-truth comparison excludes it too (like-for-like)",
      all(v == 0 or True for v in budget.by_provider_day(kind="batch").values())
      and 54.51 not in [round(v, 2) for v in budget.by_provider_day(kind="batch").values()])
q = budget.quarantined_since(DAY)
check("it is listed as quarantined, with the amount", q and abs(q[0]["cost"] - 54.51) < 1e-9)
check("and carries its project so attribution stays explainable", q[0]["project"] == "saga")

print("-- the receipt SHOWS the exclusion rather than silently dropping it --")
lines = receipt._quarantine_lines()
check("the receipt emits an exclusion block when a quarantined row exists", len(lines) >= 2)
check("it states the excluded AMOUNT (the reader must be able to add it back)", "$54.51" in "\n".join(lines))
table = receipt._two_axis_table(receipt.tally())
tot_row = [ln for ln in table if ln.startswith("TOTAL")]
check("the excluded amount is NOT inside the TOTAL row", tot_row and "54.51" not in tot_row[0], str(tot_row))
check("and the exclusion block sits after the total, not before it",
      table.index(tot_row[0]) < min(i for i, ln in enumerate(table) if "EXCLUDED" in ln))

print("-- the operator view exposes the arithmetic instead of auto-repairing --")
sus = budget.suspect_batches(DAY)
check("the suspect row is listed with its token count", any(s["ts"] == ts for s in sus))
from spendguard import cli
check("`spendguard quarantine` is dispatched", 'cmd == "quarantine"' in inspect.getsource(cli._dispatch))
check("listing is READ-ONLY — it reports, it does not tag anything",
      budget.suspect_batches(DAY) is not None
      and len(budget.quarantined_since(DAY)) == len(q))

print("-- EVERY cost aggregator excludes quarantine (the exclusion must not depend on remembering) --")
# The first pass fixed spent_since and by_provider_day and MISSED by_day (the leak check) and by_dims (the
# SaaS push payload) — so the leak view still counted the invented $54.51, and the org dashboard would have
# received it. One reader that forgets is the whole bug back. This scans the module instead of trusting a list.
import re as _re
bsrc = pathlib.Path(inspect.getfile(budget)).read_text()
_fns = _re.split(r"\ndef ", bsrc)
missing = []
for blk in _fns:
    name = blk.split("(")[0].strip()
    if "SUM(cost)" not in blk or "FROM charges" not in blk:
        continue
    if name in ("quarantined_since", "suspect_batches", "meta_spent_since"):
        continue                      # these are ABOUT quarantined/meta rows, or scoped to kind='meta'
    if "QUARANTINE_CONV" not in blk:
        missing.append(name)
check("no cost aggregator sums charges without excluding quarantine", not missing, f"missing in: {missing}")

print("-- and the aggregators still RUN (a placeholder/arg mismatch is a silent SQL error) --")
for _f, _kw in ((budget.by_provider_day, {"kind": "batch"}), (budget.gate_by_project_day, {"kind": "batch"}),
                (budget.gate_batch_cells, {}), (budget.by_key, {}), (budget.by_dims, {}),
                (budget.by_day, {"kind": "batch"})):
    try:
        _f(**_kw)
        ok = True
    except Exception as _e:
        ok = False
    check(f"{_f.__name__} executes", ok)

print("-- the leak check alarms in BOTH directions (it only ever watched one) --")
# A ledger that alarms on missing money but never on invented money reports a clean bill while claiming 2×
# what the provider billed. That is precisely how the impossible $54.51 went unnoticed for three days.
from spendguard import ledger_sync
csrc = inspect.getsource(ledger_sync)
check("_compute measures overhang, not just leak", "overhang" in inspect.getsource(ledger_sync._compute))
check("the same materiality applies both ways (no softer bar for over-counting)",
      "material = max(2.0, 0.03 * post_p)" in csrc)
lr = inspect.getsource(ledger_sync._render_leak_line)
check("the one-line status can report over-coverage", "ABOVE provider" in lr)
# and it is arithmetic, not opinion: overhang = accounted − provider, floored at zero
over = ledger_sync._compute.__doc__ is not None
c_over = {"post_p": 100.0, "post_l": 150.0, "leak": 0.0, "overhang": 50.0, "coverage": 150.0,
          "capture_rate": 100.0, "cutoff": "2026-08-01", "pre_ledger": 0.0}
line = ledger_sync._render_leak_line(c_over)
check("a 150%-accounted ledger does NOT read as '✓ no material leak'", "✓" not in line, line)
check("…it names the amount above provider truth", "$50.00" in line, line)
c_ok = dict(c_over, post_l=100.0, overhang=0.0, coverage=100.0)
check("a matching ledger still reads clean", "✓" in ledger_sync._render_leak_line(c_ok))

print(f"\n{'[FAIL]' if failures else 'OK'} test_estimate_plausibility: {failures} failure(s)")
sys.exit(1 if failures else 0)
