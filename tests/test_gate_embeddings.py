"""Embeddings capture (gate) — the two former blind spots, made un-regressable:
  • realtime `client.embeddings.create` is INTERCEPTED (estimated, budget-accounted, recorded) —
    it used to be invisible: not patched, not in the corpus, not provider-reconcilable without an
    admin key;
  • batch JSONL bodies that carry `input` (embeddings / Responses-style) are ESTIMATED — they used
    to count $0 input, so the cap could never see an embeddings batch coming.
Offline (stubbed SDK method, dead-proxy env), zero spend. Prices come from the REAL pricing table."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-emb-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
import types
from spendguard import gate as spend_gate
from spendguard import pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


EMB = "text-embedding-3-small"
P = pricing.price(EMB)                              # expectations derive from the real table

# ── estimator: string / list-of-strings / pre-tokenized inputs; output ceiling always 0 ──
m, n, out = spend_gate._est_oai_embeddings(dict(model=EMB, input="hello world, embed me"))
ck("string input estimates >0 input tokens, out=0", m == EMB and n > 0 and out == 0)
m, n2, _ = spend_gate._est_oai_embeddings(dict(model=EMB, input=["chunk one", "chunk two", "chunk three"]))
ck("list-of-strings sums the chunks", n2 > n)
m, n3, _ = spend_gate._est_oai_embeddings(dict(model=EMB, input=[[1, 2, 3, 4, 5], [6, 7, 8]]))
ck("pre-tokenized int arrays count exactly (8 ids)", n3 == 8)

# ── actuals reader: embeddings usage has prompt_tokens only ──
r = types.SimpleNamespace(usage=types.SimpleNamespace(prompt_tokens=123))
ck("actuals = (prompt_tokens, 0)", spend_gate._act_oai_embeddings(r) == (123, 0))
ck("no usage → None (skip, never guess)", spend_gate._act_oai_embeddings(types.SimpleNamespace()) is None)

# ── the surface is REGISTERED and actually PATCHED after install() ──
regs = [(s[0], s[1]) for s in spend_gate.RT_INTERCEPTORS]
ck("Embeddings + AsyncEmbeddings registered as realtime surfaces",
   ("openai.resources.embeddings", "Embeddings") in regs and ("openai.resources.embeddings", "AsyncEmbeddings") in regs)

from openai.resources import embeddings as _oe
_oe.Embeddings.create = lambda self, *a, **k: types.SimpleNamespace(
    model=EMB, usage=types.SimpleNamespace(prompt_tokens=100_000))
spend_gate.install()                                 # wraps the freshly-stubbed method
ck("embeddings.create IS gated after install()", getattr(_oe.Embeddings.create, "_spend_gated", False) is True)

import openai
client = openai.OpenAI(api_key="test-key-not-real")
for k in ("GATE_ALLOW", "GATE_DISABLE"):
    os.environ.pop(k, None)
os.environ["GATE_RT_BUDGET"] = "1000"
spend_gate._rt_spent = 0.0
client.embeddings.create(model=EMB, input=["some text to embed"])
expected = 100_000 * P["in_"] / 1e6                  # actual usage × table price, out=0
ck(f"a realtime embeddings call is ACCOUNTED (${expected:.4f} at table price)",
   abs(spend_gate._rt_spent - expected) < 1e-9)

# ── budget enforcement covers embeddings too ──
os.environ["GATE_RT_BUDGET"] = f"{expected * 0.5:.6f}"   # budget below what's already spent → next call refused
refused = False
try:
    client.embeddings.create(model=EMB, input=["more text"])
except spend_gate.SpendGateRefused:
    refused = True
ck("over the realtime budget, an embeddings call is REFUSED like any other", refused)
os.environ["GATE_RT_BUDGET"] = "1000"

# ── batch JSONL: `input` bodies now estimate real $ (they used to read $0) ──
lines = [
    json.dumps({"custom_id": "a", "body": {"model": EMB, "input": "alpha " * 200}}),
    json.dumps({"custom_id": "b", "body": {"model": EMB, "input": ["beta " * 100, "gamma " * 100]}}),
    json.dumps({"custom_id": "c", "body": {"model": EMB, "input": [[1] * 500]}}),
]
est = spend_gate._estimate_openai_jsonl("\n".join(lines).encode())
ck("embedding batch estimates >0 input tokens across all 3 body shapes",
   est["requests"] == 3 and est["in_tok"] >= 500 and est["out_tok"] == 0)
ck("embedding batch priced at the BATCH rate from the table",
   abs(est["cost"] - pricing.batch_cost(EMB, est["in_tok"], 0)) < 1e-9 and est["cost"] > 0)
chat_line = json.dumps({"body": {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi there"}],
                                 "max_tokens": 50}})
est2 = spend_gate._estimate_openai_jsonl((lines[0] + "\n" + chat_line).encode())
ck("mixed file: message bodies still counted alongside input bodies",
   est2["requests"] == 2 and est2["out_tok"] == 50 and est2["in_tok"] > 200)

# ── batched embeddings: the SUM may exceed the window, but each ITEM must not (the quarantine bug) ──
# WHAT WENT WRONG: client.embeddings.create(model=…, input=[…1000 strings…]) bills the SUM of the list, but the
# context window (8,191) bounds each ITEM. The impossibility rail computed per_req = sum/1_request, saw sum > window,
# declared IMPOSSIBLE and QUARANTINED a legitimate batch — recording $0 and losing the vectors. The decision is
# OBSERVED here at its single source (_budget_record's `quarantine` arg), which is computed identically under any
# storage backend, so this proves the real post-call accounting path (_rt_account → _embed_per_item_max →
# _record_rt → _rt_record → _implausible_estimate) reaches the right verdict — not just the leaf function.
EMB_WINDOW = 8_191                                   # OpenAI text-embedding-3 family context window (a named bound)
pricing.CONTEXT_LIMITS[EMB] = {"max_input_tokens": EMB_WINDOW}
ck("the embeddings window resolves, so the rail is LIVE (not silently disabled by an absent limit)",
   pricing.max_input_tokens(EMB) == EMB_WINDOW)

_orig_br = spend_gate._budget_record
_cap = []
def _capture_br(cost, model, provider, kind, quarantine=False, basis=None):
    _cap.append({"kind": kind, "cost": cost, "quarantine": quarantine})
    return _orig_br(cost, model, provider, kind, quarantine=quarantine, basis=basis)
spend_gate._budget_record = _capture_br

def _resp(inp):                                      # a real embeddings response: usage.prompt_tokens = the batch SUM
    return types.SimpleNamespace(model=EMB, usage=types.SimpleNamespace(
        prompt_tokens=spend_gate._est_oai_embeddings(dict(model=EMB, input=inp))[1]))
def _decide(inp):                                    # drive the real post-call accounting; return its realtime rows
    _cap.clear()
    spend_gate._rt_account(EMB, dict(model=EMB, input=inp), _resp(inp),
                           spend_gate._est_oai_embeddings, spend_gate._act_oai_embeddings)
    return [c for c in _cap if c["kind"] == "realtime"]

SENTENCE = "The quick brown fox jumps over the lazy dog and then trots quietly home. "
BATCH = [SENTENCE] * ((EMB_WINDOW // max(spend_gate._ct(SENTENCE), 1) + 1) * 3)   # SUM ≫ window, each item ≪ window
SUM_A = spend_gate._est_oai_embeddings(dict(model=EMB, input=BATCH))[1]
ck("precondition: the batch SUM exceeds the window while each item is far under it",
   SUM_A > EMB_WINDOW and spend_gate._embed_per_item_max(dict(input=BATCH)) <= EMB_WINDOW)
ck("precondition: the rail would fire on the SUM alone (proving per-item logic is what saves the batch)",
   spend_gate._implausible_estimate(EMB, SUM_A, 1)[0] is True)

rtA = _decide(BATCH)
ck("a batched embeddings call is accounted as ONE realtime row", len(rtA) == 1)
ck("…and is NOT quarantined (the fixed bug — a 1000-string batch is legitimate)",
   bool(rtA) and rtA[0]["quarantine"] is False)
ck("…and its cost is the real batch SUM at the table price (billing preserved end-to-end)",
   bool(rtA) and abs(rtA[0]["cost"] - pricing.realtime_cost(EMB, SUM_A, 0)) < 1e-9)

rtB = _decide(["word " * (EMB_WINDOW + 500)])        # ONE string whose token count exceeds the window
ck("a single OVER-window item IS still quarantined (the rail did not weaken)",
   bool(rtB) and rtB[0]["quarantine"] is True)
rtC = _decide(list(range(EMB_WINDOW + 50)))          # ONE pre-tokenized document longer than the window (list[int])
ck("an over-window pre-tokenized doc (list[int]) IS quarantined too (the len-not-1 special case, live)",
   bool(rtC) and rtC[0]["quarantine"] is True)

spend_gate._budget_record = _orig_br                 # restore the real writer

# ── the SAME per-item rule holds on the BATCH surface (a batch JSONL of embeddings bodies), not just realtime ──
# The impossibility rail on the batch estimator used per_request = in_tok/requests (an AVERAGE) — for a batch of
# embeddings lines each carrying a big list, that average exceeds the window even though every ITEM fits, so the
# whole batch was quarantined. The estimator now bounds by the largest single request/item, never the batch sum.
LINE_ITEMS = (EMB_WINDOW // max(spend_gate._ct(SENTENCE), 1)) + 20     # each line's SUM alone exceeds the window
big_batch = "\n".join(json.dumps({"custom_id": f"e{i}", "body": {"model": EMB, "input": [SENTENCE] * LINE_ITEMS}})
                      for i in range(4)).encode()
est_ok = spend_gate._estimate_openai_jsonl(big_batch)
ck("precondition: the batch's per-request AVERAGE exceeds the window (the old rail WOULD have quarantined it)",
   est_ok["in_tok"] / est_ok["requests"] > EMB_WINDOW)
ck("a legit embeddings batch (big lists, small items) is NOT flagged implausible (batch surface fixed)",
   not est_ok["implausible"] and est_ok["in_tok"] > EMB_WINDOW)
est_bad = spend_gate._estimate_openai_jsonl(
    json.dumps({"custom_id": "z", "body": {"model": EMB, "input": ["word " * (EMB_WINDOW + 500)]}}).encode())
ck("a batch line whose single item exceeds the window IS flagged implausible (rail intact on the batch surface)",
   bool(est_bad["implausible"]))
est_pretok = spend_gate._estimate_openai_jsonl(
    json.dumps({"custom_id": "p", "body": {"model": EMB, "input": [list(range(EMB_WINDOW + 50))]}}).encode())
ck("a pre-tokenized batch doc (list[list[int]]) over the window IS flagged implausible too",
   bool(est_pretok["implausible"]))

print(("[OK]" if not fails else "[FAIL]") + " gate embeddings: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
