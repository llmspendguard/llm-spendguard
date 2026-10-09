"""The `audit --deep` price auditor must hand the judge the WHOLE canonical price table, never a prefix slice. The bug
it guards: deep() was called with repr(PRICING)[:4000] — 0.9% of a 450KB / 3,634-model table — so any hardcoded price
for a model past the first 0.9% read as clean (a NEVER-TRUNCATE-the-evidence violation). The fix sends the whole table,
chunked only to fit the window, and lets the JUDGE resolve which models a file references. This test FAILS if the
evidence handed to the deep auditor is a proper prefix of the canonical table, or if any entry is dropped.
Offline: adapters.call + pricing/window are stubbed; no model call, no spend."""
import ast
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-audit-deep-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import audit, adapters, pricing, expected_output  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# a table big enough that its repr far exceeds the old 4000-char slice, with a TAIL entry well past it
BIG = {f"model-{i:04d}": (round(0.1 * i, 4), round(0.2 * i, 4)) for i in range(2000)}
TAIL = "model-1999"
ck("precondition: the tail model is OUTSIDE the old [:4000] prefix", repr(BIG).index(TAIL) > 4000)

# ── 1. _price_table_chunks: every entry is seen across chunks (nothing dropped), and it's not one prefix ──
chunks = audit._price_table_chunks(BIG, 1000)
seen = set()
for ch in chunks:
    seen |= set(ast.literal_eval(ch).keys())
ck("_price_table_chunks: union of chunks == the WHOLE table (no entry dropped)", seen == set(BIG))
ck("_price_table_chunks: more than one chunk when the budget is small (it chunks, not truncates)", len(chunks) > 1)

# ── 2. deep(): the WHOLE table reaches the judge across calls — the tail model's rate is present, nothing is a prefix ──
_captured = []
def _fake_call(model, prompt, **kw):
    _captured.append(prompt)
    return {"text": '{"hardcoded_prices": []}', "error": None}
adapters.call = _fake_call
pricing.max_input_tokens = lambda m: 5000          # small window → force multi-chunk coverage
pricing.realtime_cost = lambda *a, **k: 0.0
expected_output.expect = lambda *a, **k: (100, "stub")
audit._cfg_advisor = lambda: "gpt-4.1-mini"

_f = os.path.join(os.environ["SPENDGUARD_HOME"], "suspect.py")
with open(_f, "w") as fh:
    fh.write('PRICE = {"model-1999": (999.0, 999.0)}   # a hardcoded rate for a TAIL model\n')

audit.deep([_f], BIG, run=True)
_all = "\n".join(_captured)
ck("deep sent the TAIL model's canonical entry (the exact hardcode-outside-0.9% case is now findable)",
   TAIL in _all and repr(BIG[TAIL]) in _all)
missing = [k for k in BIG if k not in _all]
ck("deep sent EVERY canonical entry across its calls (evidence complete, not truncated)", not missing)
# the old bug sent exactly repr(PRICING)[:4000]; proving the tail IS present AND nothing is missing is precisely the
# negation of 'the evidence is a proper prefix of the canonical table'.
ck("the evidence is NOT the old 0.9% prefix (tail present + nothing dropped ⇒ not a prefix slice)",
   (TAIL in _all) and not missing)

print(f"\n{'[FAIL]' if fails else 'OK'} test_audit_deep_evidence_whole: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
