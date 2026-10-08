"""The batch-output contamination fix: a learned 'per-request' output that exceeds the model's output CEILING is
physically impossible for one request (it is a per-shard/packed total taught as per-request), so expect() must
DISCARD it — not silently degrade the inflated value to the ceiling, which over-states the batch estimate ~shard_size×
and false-refuses every shard. And note_response normalizes a packed/batch total to per-item so it never contaminates.
Offline (stubbed maxtokens/pricing for the expect path; isolated gate db for note_response). Zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-contam-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import expected_output as eo, bulkgate, pricing, models  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# ── 1. expect() DISCARDS a learned p90 above the model ceiling (contaminated), falls to a trustworthy basis ──
pricing.max_output_tokens = lambda m: 128000
models.reasons_by_default = lambda m: False          # so the fall-through lands on model-max, not reasoning-floor
bulkgate.model_outputs = lambda m: {}                # no broad history → fall-through continues past RUNG 2

bulkgate.maxtokens = lambda sig, **k: {"n": 30, "p90": 339897}   # a 60-shard total taught as 'per-request'
eo._contam_warned.clear()
tok, basis = eo.expect("gpt-6-sol", sig="snomed-onlyhome-confirm")
ck("a learned p90 > model ceiling is NOT returned as 'learned' (discarded as contaminated)", basis != "learned")
ck("the contamination was flagged loudly (recorded once per scope)", "sig:snomed-onlyhome-confirm" in eo._contam_warned)
ck("it falls through to a trustworthy basis, not a silent inflated 'learned'", basis in ("model-history", "model-max", "declared", "reasoning-floor", "unknown"))

bulkgate.maxtokens = lambda sig, **k: {"n": 30, "p90": 5000}     # a SANE per-request measurement
tok2, basis2 = eo.expect("gpt-6-sol", sig="normal-intent")
ck("a sane learned p90 (<= ceiling) is still used as 'learned'", basis2 == "learned" and tok2 == 5000)

# a measured broad (model-history) p90 above the ceiling is also discarded
bulkgate.maxtokens = lambda sig, **k: {"n": 0, "p90": None}       # no class history → RUNG 2
bulkgate.model_outputs = lambda m: {"n": 40, "p90": 300000}      # contaminated model-wide too
eo._contam_warned.clear()
_t3, basis3 = eo.expect("gpt-6-sol", sig="x2")
ck("a model-history p90 > ceiling is also discarded (not 'model-history')", basis3 != "model-history")

# ── 2. note_response normalizes a packed/batch total to PER-ITEM so maxtokens learns per-request ──
# restore the real maxtokens/model_outputs for the gate-db path
import importlib
importlib.reload(bulkgate)
SIG = bulkgate.sig("gpt-6-sol", template_id="snomed-onlyhome-confirm")
for _ in range(25):                                   # > MIN_OBS; a 60-request shard emitting 231,352 total
    bulkgate.note_response(SIG, "gpt-6-sol", 231352, max_tokens=128000, finish_reason="stop", n_items=60)
mt = bulkgate.maxtokens(SIG)
ck("note_response(n_items=60) records PER-ITEM, so learned p90 is ~3,855 not ~231k",
   mt.get("p90") is not None and mt["p90"] < 10000)
# a normal single call (n_items=1) is unchanged
for _ in range(25):
    bulkgate.note_response(bulkgate.sig("m", template_id="solo"), "m", 4000, n_items=1)
mt1 = bulkgate.maxtokens(bulkgate.sig("m", template_id="solo"))
ck("a single call (n_items=1) records the whole out_tok unchanged", mt1.get("p90") == 4000)

print(f"\n{'[FAIL]' if fails else 'OK'} test_batch_output_contamination: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
