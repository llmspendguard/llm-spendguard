"""First-class embedding surface: adapters.embed() (realtime, robust bulk) + embed_batch() (async Batch API) +
embed_compare() (openai-vs-gemini A/B).

The gap it closes: callers hand-rolled embedding requests (semcache/equivalence use the raw SDK; 7thsense built its
own batch), so there was no gated, chunked, resumable, cross-provider embed surface — the same hand-rolling that
produced the reasoning_effort batch bug. Guards:
  • embed() returns vectors ALIGNED to inputs; a MULTI-chunk run auto-checkpoints (durable) and RESUMES; an OVERSIZED
    input is marked failed (not sent); one chunk's failure is ISOLATED (the rest still embed); a deliberate spend
    stop PROPAGATES; it routes cross-provider by the model id (openai default, gemini/voyage by id);
  • embed_batch() builds a /v1/embeddings JSONL and gates it; a provider without the `batch` capability errors LOUDLY;
  • submit.estimate_jsonl_cost prices an embeddings batch by input tokens (out=0);
  • embed_compare() A/Bs two models and reports pairwise neighbourhood-structure agreement.
Offline: the embeddings client is stubbed; embed_batch runs submit=False (no network).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-embed-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, submit, gate

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


class _Row:
    def __init__(self, index, embedding):
        self.index, self.embedding = index, embedding


class _Resp:
    def __init__(self, vecs):
        self.data = [_Row(i, v) for i, v in enumerate(vecs)]


class _FakeClient:
    """Stands in for the OpenAI client: .embeddings.create(model, input, **kw) → one 3-float vector per input.
    Records each chunk's inputs (to prove resume/chunking) and can raise on a chunk containing a marker."""
    def __init__(self, raise_on=None, stop_on=None):
        self.embeddings = self
        self.calls = []
        self._raise_on, self._stop_on = raise_on, stop_on

    def create(self, model=None, input=None, **kw):
        self.calls.append(list(input))
        if self._stop_on and any(self._stop_on in s for s in input):
            raise gate.SpendGateRefused("refused mid-embed")
        if self._raise_on and any(self._raise_on in s for s in input):
            raise RuntimeError("provider 500 on this chunk")
        return _Resp([[float(len(s)), 1.0, 2.0] for s in input])


# ── single chunk: aligned vectors, no checkpoint file ──
adapters._oai_compat_client = lambda prov, timeout_s=None: _FakeClient()
r = adapters.embed(["a", "bb", "ccc"], "text-embedding-3-small")
ck("embed() returns one vector per input, in order", len(r["vectors"]) == 3 and r["vectors"][1][0] == 2.0 and r["error"] is None)
ck("a small (single-chunk) embed does not force a checkpoint", r["checkpoint"] is None)

# ── cross-provider routing by the model id ──
_seen = {}
adapters._oai_compat_client = lambda prov, timeout_s=None: _seen.setdefault("prov", prov) or _FakeClient()
adapters.embed(["x"], "gemini-embedding-001")
ck("a gemini model id routes to the gemini provider", _seen.get("prov") == "gemini")

# ── multi-chunk: auto-checkpoint (durable) + RESUME (a re-run re-embeds nothing) ──
fake = _FakeClient()
adapters._oai_compat_client = lambda prov, timeout_s=None: fake
texts = [f"doc-{i}" for i in range(5)]
r = adapters.embed(texts, "text-embedding-3-small", max_batch=2)          # 5 items / 2 = 3 chunks → multi-chunk
ck("multi-chunk embed auto-creates a durable checkpoint", bool(r["checkpoint"]) and os.path.exists(r["checkpoint"]))
ck("multi-chunk embed returns all vectors aligned", len(r["vectors"]) == 5 and all(v is not None for v in r["vectors"]))
fake2 = _FakeClient()
adapters._oai_compat_client = lambda prov, timeout_s=None: fake2
r2 = adapters.embed(texts, "text-embedding-3-small", max_batch=2, checkpoint=r["checkpoint"])
ck("a re-run with the checkpoint RE-EMBEDS NOTHING (all served from the checkpoint)", fake2.calls == [] and all(v is not None for v in r2["vectors"]))

# ── oversized input: marked failed, never sent; the rest still embed ──
adapters._oai_compat_client = lambda prov, timeout_s=None: _FakeClient()
big = "z" * (adapters._EMBED_MAX_INPUT_CHARS + 1)
r = adapters.embed(["ok1", big, "ok2"], "text-embedding-3-small", max_batch=10)
ck("an oversized input is marked failed (not sent)", any(f["i"] == 1 for f in r["failed"]) and r["vectors"][1] is None)
ck("...and the other inputs still embed", r["vectors"][0] is not None and r["vectors"][2] is not None)
ck("...and error names the unembedded count (never a silently-short list)", "unembedded" in (r["error"] or ""))

# ── chunk failure is ISOLATED: the bad chunk is marked, the rest keep going ──
adapters._oai_compat_client = lambda prov, timeout_s=None: _FakeClient(raise_on="BOOM")
r = adapters.embed(["good1", "BOOM", "good2", "good3"], "text-embedding-3-small", max_batch=2)  # chunk0=[good1,BOOM] fails
ck("a failing chunk marks its items in failed[] but does NOT abort the run",
   any(f["i"] in (0, 1) for f in r["failed"]) and r["vectors"][2] is not None and r["vectors"][3] is not None)

# ── a deliberate spend stop PROPAGATES (never isolated into failed[]) ──
adapters._oai_compat_client = lambda prov, timeout_s=None: _FakeClient(stop_on="pay")
raised = False
try:
    adapters.embed(["pay"], "text-embedding-3-small")
except gate.SpendGateRefused:
    raised = True
ck("a deliberate spend stop PROPAGATES out of embed() (never a silent [])", raised)

# ── embed_batch: builds a /v1/embeddings JSONL, gated (submit=False = estimate only) ──
rb = adapters.embed_batch(["alpha", "beta", "gamma"], "text-embedding-3-small", submit=False)
ck("embed_batch writes a durable JSONL and returns its path", rb["jsonl"] and os.path.exists(rb["jsonl"]) and rb["error"] is None)
import json as _json
_lines = [l for l in open(rb["jsonl"]).read().splitlines() if l.strip()]
_first = _json.loads(_lines[0])
ck("embed_batch JSONL lines address /v1/embeddings with an `input` body",
   len(_lines) == 3 and _first["url"] == "/v1/embeddings" and "input" in _first["body"])

# ── a provider WITHOUT the batch capability errors loudly (no silent fallback) ──
rg = adapters.embed_batch(["x"], "gemini-embedding-001", submit=False)
ck("embed_batch on a non-batch provider errors and points to embed()", rg["error"] and "embed()" in rg["error"] and rg["batch_id"] is None)

# ── submit.estimate_jsonl_cost prices an embeddings batch by INPUT tokens, out=0 (not $0) ──
est = submit.estimate_jsonl_cost(rb["jsonl"], "text-embedding-3-small", batch=True)
ck("estimate prices the embeddings batch by input (out_tok=0, cost>0)", est["out_tok"] == 0 and est["cost"] > 0 and est["requests"] == 3)

# ── Voyage registered (OpenAI-compatible /embeddings) → a voyage-* id routes to the voyage provider ──
ck("voyage-3 routes to the voyage provider (registered)", adapters.provider_for("voyage-3") == "voyage")
adapters._oai_compat_client = lambda prov, timeout_s=None: _FakeClient()
rv = adapters.embed(["hi"], "voyage-3")
ck("embed(model='voyage-3') builds a client + returns a vector", len(rv["vectors"]) == 1 and rv["error"] is None)

# ── embed_compare: A/B two models on a sample → per-model rows + pairwise STRUCTURE agreement (Pearson of sims) ──
_vecmap = {"m-a": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
           "m-b": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.85, 0.15, 0.0]]}
_real_embed = adapters.embed
adapters.embed = lambda texts, model=None, **kw: {"vectors": _vecmap[model], "dims": 3, "failed": [], "error": None}
try:
    cmp = adapters.embed_compare(["t0", "t1", "t2"], models=["m-a", "m-b"])
finally:
    adapters.embed = _real_embed
ck("embed_compare reports one row per model", len(cmp["models"]) == 2 and cmp["sample_n"] == 3)
ck("embed_compare reports pairwise structure agreement over the shared pairs",
   len(cmp["agreement"]) == 1 and cmp["agreement"][0]["pairs"] == 3 and cmp["agreement"][0]["structure_corr"] is not None)

# ── a provider that returns FEWER vectors than inputs (a short r.data) → the omitted item is in failed[], not silent ──
class _ShortClient:
    def __init__(self):
        self.embeddings = self

    def create(self, model=None, input=None, **kw):
        return _Resp([[1.0, 2.0, 3.0] for _s in input[:-1]])          # omit the LAST input's vector

adapters._oai_compat_client = lambda prov, timeout_s=None: _ShortClient()
rs = adapters.embed(["a", "b", "c"], "text-embedding-3-small", max_batch=10)
ck("a provider omitting a vector → that item is in failed[] and None in vectors (never silently short)",
   rs["vectors"][2] is None and any(f["i"] == 2 for f in rs["failed"]) and "unembedded" in (rs["error"] or ""))

print(("[OK]" if not fails else "[FAIL]") + " embed surface: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
