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

print(("[OK]" if not fails else "[FAIL]") + " gate embeddings: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
