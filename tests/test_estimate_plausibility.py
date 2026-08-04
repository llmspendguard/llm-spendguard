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

print(f"\n{'[FAIL]' if failures else 'OK'} test_estimate_plausibility: {failures} failure(s)")
sys.exit(1 if failures else 0)
