"""Per-provider embedding batch ceiling — adapters.embed() must never send a chunk larger than the provider's cap,
and must RECOVER (not fail every input) when a batch is still rejected.

THE DEFECT (2026-09-30). adapters.embed() applied ONE global batch size (_EMBED_MAX_BATCH) to every OpenAI-compatible
embedding provider. Gemini's batchEmbedContents caps at 100 inputs/request; because the 400 fails the WHOLE chunk,
a 5,768-text run on gemini-embedding-001 returned 5,760/5,768 UNEMBEDDED. The ceilings are PROVIDER-ENFORCED, not
tuning knobs, so they belong in the catalog SSOT (measured, not assumed): Gemini=100, OpenAI=2048, Voyage=1000.

THE CONTRACT this pins:
  PART 1 — the catalog accessor model_catalog.embed_batch_ceiling returns the MEASURED cap per embed model, and None
           for a chat model / an uncurated id (→ caller's own default, never a guess).
  PART 2 — CLAMP: embed(gemini, max_batch=500) never sends a chunk > 100; a smaller max_batch is honored (min, not
           force); every input embeds and stays aligned.
  PART 3 — EMPIRICAL RECOVERY: on an UNCURATED provider whose batch is still rejected, embed() BISECTS (the provider's
           accept/reject is the oracle — no error text is parsed) until the inputs fit, so ALL inputs embed instead of
           all failing, and vectors stay aligned across the re-slice.

The fake /embeddings returns a CONTENT-KEYED sentinel ([n,n,n] for input "x<n>"), so alignment is checkable no matter
how embed() chunks or re-slices. Offline: the client is faked (no key, no spend); the gate patches the real SDK class,
which the fake is not. Isolated SPENDGUARD_HOME.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-embedcap-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, model_catalog   # noqa: E402


class _Emb:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class _Resp:
    def __init__(self, data):
        self.data = data


class _RecordingEmbeddings:
    """Records each request's input size; optionally RAISES a provider-style 400 when a batch exceeds `ceiling` (to
    drive the bisection recovery). Each input "x<n>" gets the CONTENT sentinel [n,n,n], so a mismap is detectable no
    matter how embed() chunks (the vector encodes the INPUT, not its position in the request)."""
    def __init__(self, sizes, ceiling=None, msg=None):
        self.sizes = sizes
        self.ceiling = ceiling
        self.msg = msg

    def create(self, model, input, **kw):
        n = len(input)
        self.sizes.append(n)
        if self.ceiling is not None and n > self.ceiling:
            raise RuntimeError(self.msg or f"400 - at most {self.ceiling} requests can be in one batch")
        return _Resp([_Emb(j, [float(int(s[1:]))] * 3) for j, s in enumerate(input)])  # "x<n>" -> [n,n,n]


class _Client:
    def __init__(self, emb):
        self.embeddings = emb


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    # ── PART 1: the catalog accessor ──
    print("-- PART 1: model_catalog.embed_batch_ceiling — measured caps; None for chat/uncurated --")
    ck("gemini-embedding-001 → 100", model_catalog.embed_batch_ceiling("gemini-embedding-001") == 100,
       model_catalog.embed_batch_ceiling("gemini-embedding-001"))
    ck("gemini:gemini-embedding-001 (with provider prefix) → 100",
       model_catalog.embed_batch_ceiling("gemini:gemini-embedding-001") == 100)
    ck("text-embedding-3-small → 2048", model_catalog.embed_batch_ceiling("text-embedding-3-small") == 2048,
       model_catalog.embed_batch_ceiling("text-embedding-3-small"))
    ck("text-embedding-3-large → 2048", model_catalog.embed_batch_ceiling("text-embedding-3-large") == 2048)
    ck("voyage-3.5 → 1000", model_catalog.embed_batch_ceiling("voyage-3.5") == 1000,
       model_catalog.embed_batch_ceiling("voyage-3.5"))
    ck("a CHAT model → None (no embed cap)", model_catalog.embed_batch_ceiling("gpt-5") is None)
    ck("an UNCURATED id → None (caller's own default, not a guess)",
       model_catalog.embed_batch_ceiling("no-such-embed-model-xyz") is None)

    _real = adapters._oai_compat_client
    try:
        # ── PART 2: the CLAMP (gemini, the model in the incident) ──
        print("\n-- PART 2: embed() clamps every chunk to the gemini ceiling (100), honoring a smaller max_batch too --")
        sizes = []
        adapters._oai_compat_client = lambda prov, timeout_s=None: _Client(_RecordingEmbeddings(sizes))
        texts = [f"t{i}" for i in range(250)]
        r = adapters.embed(list(texts), model="gemini:gemini-embedding-001", max_batch=500, checkpoint=False)
        ck("max_batch=500 is CLAMPED to 100 — no chunk exceeds the cap", sizes and max(sizes) <= 100, max(sizes) if sizes else None)
        ck("all 250 inputs embedded (no partial loss — the incident)",
           not r.get("error") and sum(1 for v in r["vectors"] if v is not None) == 250, r.get("error"))
        ck("vectors stay ALIGNED to inputs under clamped chunking", all(r["vectors"][i] == [float(i)] * 3 for i in range(250)),
           [r["vectors"][i] for i in (0, 99, 100, 249)])

        sizes2 = []
        adapters._oai_compat_client = lambda prov, timeout_s=None: _Client(_RecordingEmbeddings(sizes2))
        adapters.embed([f"s{i}" for i in range(120)], model="gemini:gemini-embedding-001", max_batch=40, checkpoint=False)
        ck("a smaller max_batch (40) is honored (clamp is min, not force to 100)", sizes2 and max(sizes2) <= 40, max(sizes2) if sizes2 else None)

        # ── PART 3: EMPIRICAL RECOVERY (bisection) on an uncurated provider that still rejects the batch ──
        print("\n-- PART 3: an UNCURATED model that rejects the batch → embed() BISECTS, all inputs embed, aligned --")
        sizes3 = []
        adapters._oai_compat_client = lambda prov, timeout_s=None: _Client(
            _RecordingEmbeddings(sizes3, ceiling=50, msg="400 - at most 50 requests can be in one batch"))
        # openai:<uncurated> → provider resolves (openai) but the model has NO catalog ceiling → no clamp → default 128
        rr = adapters.embed([f"u{i}" for i in range(130)], model="openai:uncurated-embed-x", max_batch=128, checkpoint=False)
        ck("the default 128 batch was attempted first (no clamp for an uncurated model)", 128 in sizes3, sizes3[:3])
        ck("it BISECTED — some request fit under the provider's cap (<=50)", any(0 < s <= 50 for s in sizes3), sizes3)
        ck("ALL 130 inputs embedded via bisection (none failed)",
           not rr.get("error") and sum(1 for v in rr["vectors"] if v is not None) == 130, rr.get("error"))
        ck("bisected vectors stay ALIGNED to inputs (no mismap on re-slice)",
           all(rr["vectors"][i] == [float(i)] * 3 for i in range(130)),
           [rr["vectors"][i] for i in (0, 49, 50, 100, 129)])
    finally:
        adapters._oai_compat_client = _real

    print(f"\n{'[FAIL]' if fails else 'OK'} test_embed_batch_ceiling: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
