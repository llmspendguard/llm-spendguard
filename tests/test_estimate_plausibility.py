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
from spendguard import ledger as _L
ts = next(r["ts_utc"] for r in budget._ledger().query() if _L.to_dec(r.get("batch_usd")) == _L.to_dec("54.51"))
n = budget.quarantine_charge(ts, "test: base64-as-tokens")
check("exactly one row is tagged", n == 1, f"{n}")
check("re-running is a no-op (idempotent)", budget.quarantine_charge(ts, "again") == 0)
check("it drops out of the spend total", abs(budget.spent_since(DAY) - (plain - 54.51)) < 0.005,
      f"{budget.spent_since(DAY)} vs {plain - 54.51}")
row = next((r for r in budget._ledger().query(where={"ts_utc": ts})), None)
check("the ROW still exists with its amount intact (forensics, not deletion)",
      row is not None and _L.to_dec(row["batch_usd"]) == _L.to_dec("54.51") and row["model"] == MODEL)
check("…tagged as quarantined (status void)", row["status"] == "void")
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

print("-- realtime is cross-checked WITHOUT an admin key (real use must never need one) --")
from spendguard import ledger_sync as _ls
_lsrc = inspect.getsource(_ls.realtime_check)
check("the comparator is the gate's OWN log, not a provider admin API",
      "RT_LOG" in _lsrc and "admin" not in _lsrc.lower().replace("no admin", "").replace("admin key", ""))
check("it measures BOTH directions", "over" in _lsrc and "under" in _lsrc)
c = _ls.realtime_check(100.0, since="2000-01-01")
check("a ledger above its own log reports the overhang",
      c["log"] is None or c["over"] >= 0)
c2 = {"recorded": 10.0, "log": 100.0}
check("windows must match — a month of ledger vs an all-time log is the wolf-crying bug",
      "MUST match" in inspect.getdoc(_ls.realtime_check) or "same window" in _lsrc)

print("-- a day whose number is a spread ARTIFACT is not labelled as a finding --")
_dsrc = inspect.getsource(_ls.sync)
check("days carrying backfill are named not-comparable", "backfill spread across days" in _dsrc)
check("…and only stand-alone days get an over/under verdict",
      '"under-covered"' in _dsrc and '"over-covered"' in _dsrc and "spread = " in _dsrc)
print("-- EMBEDDINGS: a batch bills the SUM but the window bounds each ITEM (the batched-embedding bug) --")
# WHAT WENT WRONG: client.embeddings.create(model=…, input=[…1000 strings…]) bills the SUM of the list, but
# every embeddings model's context window (8,191) applies PER ITEM. The rail computed per_req = sum/1_request,
# saw sum > 8,191, declared IMPOSSIBLE, and QUARANTINED a legitimate batch — so a bake-off that embedded 1000
# short strings recorded $0 and lost its vectors. The fix threads per_item_max (the largest single input) so
# the bound is checked against the ITEM, never the batch total. text-embedding-3-large is a REAL model already
# priced in pricing._FALLBACK — the price is read from there, never a literal; only its window is seeded here.
# (The ledger end-to-end — a real batch is recorded, not voided — lives in test_batched_embedding_not_quarantined.py,
#  which runs under the sqlite backend; the quarantine tag only persists there, not under this file's memory backend.)
EMB = "text-embedding-3-large"
EMB_WINDOW = 8_191            # OpenAI's published context window for the text-embedding-3 family (a named bound)
pricing.CONTEXT_LIMITS[EMB] = {"max_input_tokens": EMB_WINDOW}     # MODEL's 1M limit is left intact for earlier checks
check("the embeddings model's window resolves", pricing.max_input_tokens(EMB) == EMB_WINDOW)
check("…and it is priced from the built-in table, not invented", pricing.realtime_cost(EMB, 10_000, 0) > 0)

# Fixtures built from the REAL token counter, so no token count is ever hardcoded; the SUM must exceed the
# window while every item stays under it — that is the exact shape the old rail wrongly rejected.
SENTENCE = "The quick brown fox jumps over the lazy dog and then trots quietly back home. "
_unit_ct = gate._ct(SENTENCE)
N = (EMB_WINDOW // max(_unit_ct, 1) + 1) * 3          # enough items that the batch SUM comfortably clears the window
kw = {"model": EMB, "input": [SENTENCE] * N}
SUM = gate._est_oai_embeddings(kw)[1]                 # the billable total (what the provider actually charges)
MAXITEM = gate._embed_per_item_max(kw)               # the largest single input — the quantity the window bounds
BIG = "word " * (EMB_WINDOW + 500)                    # ONE string whose token count exceeds the window
big_ct = gate._embed_per_item_max({"model": EMB, "input": [BIG]})
check("precondition: the batch SUM exceeds the window (so the old sum-based check WOULD fire)", SUM > EMB_WINDOW, f"{SUM}")
check("precondition: every item fits the window (so the batch is legitimate)", MAXITEM <= EMB_WINDOW, f"{MAXITEM}")
check("precondition: the single big string exceeds the window", big_ct > EMB_WINDOW, f"{big_ct}")

print("-- _embed_per_item_max sizes the LARGEST single input, per input shape --")
check("a str is its own token count", gate._embed_per_item_max({"input": SENTENCE}) == gate._ct(SENTENCE))
check("a list[str] is the LARGEST item, never the sum",
      gate._embed_per_item_max({"input": ["a", SENTENCE, "bb"]}) == gate._ct(SENTENCE))
check("a bare list[int] is ONE pre-tokenized doc → len (NOT maxed to 1)",
      gate._embed_per_item_max({"input": [1, 2, 3, 4, 5]}) == 5)
check("a list[list[int]] is the largest doc's length",
      gate._embed_per_item_max({"input": [[1, 2], [1, 2, 3, 4], [9]]}) == 4)
check("no input → None (no opinion)", gate._embed_per_item_max({}) is None)
check("empty list → 0", gate._embed_per_item_max({"input": []}) == 0)

print("-- the rail, on the SAME batch, with and without the per-item bound (the fix is load-bearing) --")
check("WITHOUT per_item_max the sum-based rail wrongly quarantines the legit batch (this IS the bug)",
      gate._implausible_estimate(EMB, SUM, 1)[0] is True)
check("WITH per_item_max the same batch is judged plausible (the fix)",
      gate._implausible_estimate(EMB, SUM, 1, per_item_max=MAXITEM)[0] is False)
bad_e, facts_e = gate._implausible_estimate(EMB, big_ct, 1, per_item_max=big_ct)
check("a single item past the window is still impossible (rail intact)", bad_e is True)
check("…and its message says the SUM is fine, so the operator fixes the ITEM not the batch size",
      "batch SUM" in facts_e.get("message", ""), facts_e.get("message"))
check("the per-item bound is inclusive (== window is fine)",
      gate._implausible_estimate(EMB, EMB_WINDOW * 5, 1, per_item_max=EMB_WINDOW)[0] is False)
check("one token past the per-item bound fires",
      gate._implausible_estimate(EMB, EMB_WINDOW * 5, 1, per_item_max=EMB_WINDOW + 1)[0] is True)

print(f"\n{'[FAIL]' if failures else 'OK'} test_estimate_plausibility: {failures} failure(s)")
sys.exit(1 if failures else 0)
