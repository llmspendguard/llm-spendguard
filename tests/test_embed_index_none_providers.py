"""#4 — adapters.embed must not CRASH on gemini/voyage, and must ALIGN vectors to inputs on every OpenAI-compat provider.

THE DEFECT. `vecs = {int(d.index): ...}` assumed every /embeddings item carries a chunk-relative integer `index`.
OpenAI does; gemini and voyage return `index=None`, so `int(None)` raised TypeError and EVERY embedding call to those
providers crashed (found live by the consumer×provider smoke — gemini embed, then voyage which was never reached before).

THE CONTRACT. OpenAI-compat /embeddings returns `data` in INPUT ORDER (documented), and the stamped `index` — when a
provider sets it — equals that position. So the fix keys by the stamped index when present, else the enumeration
position. This asserts BOTH: a None-index provider aligns by position (no crash, correct mapping), AND an in-order-but-
index-stamped provider that returns data OUT OF ORDER still aligns by its index (the reason index exists). Alignment is
the real stake: a mismapping would hand back the wrong vector for a text — a silent-wrong, the worst kind. Offline: the
OpenAI-compat client is faked, so no key and no spend; the gate patches the real SDK class, which the fake is not.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-embedidx-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters   # noqa: E402


class _Emb:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class _Resp:
    def __init__(self, data):
        self.data = data


class _Embeddings:
    """A fake /embeddings endpoint. `mode` chooses how it stamps index + orders data — the provider behaviours we must
    survive. Each input at position j gets the DISTINCT sentinel vector [j, j, j], so a misalignment is detectable."""
    def __init__(self, mode):
        self.mode = mode

    def create(self, model, input, **kw):
        n = len(input)
        if self.mode == "none":                       # gemini / voyage: no index, data in input order
            return _Resp([_Emb(None, [float(j)] * 3) for j in range(n)])
        if self.mode == "reversed_indexed":           # index stamped, data returned OUT OF ORDER (why index exists)
            return _Resp([_Emb(j, [float(j)] * 3) for j in reversed(range(n))])
        return _Resp([_Emb(j, [float(j)] * 3) for j in range(n)])   # openai: index in order


class _Client:
    def __init__(self, mode):
        self.embeddings = _Embeddings(mode)


def ck(results, label, cond, extra=""):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _aligned(vectors):
    """Every input at position j must map to its own sentinel [j,j,j] — the alignment invariant."""
    return all(v == [float(j)] * 3 for j, v in enumerate(vectors))


def main():
    results = []
    _real = adapters._oai_compat_client
    texts = ["alpha", "bravo", "charlie", "delta"]
    try:
        for mode, model, why in (("none", "gemini:gemini-embedding-001", "gemini/voyage: index=None"),
                                 ("none", "voyage:voyage-3.5", "voyage: index=None"),
                                 ("reversed_indexed", "openai:text-embedding-3-small", "index-stamped but out of order"),
                                 ("indexed", "openai:text-embedding-3-small", "index in order")):
            adapters._oai_compat_client = (lambda m: (lambda prov, timeout_s: _Client(m)))(mode)
            r = adapters.embed(list(texts), model=model)
            ck(results, f"{why}: no crash, no error", not r.get("error"), extra=str(r.get("error")))
            ck(results, f"{why}: {len(texts)} vectors returned", len(r.get("vectors") or []) == len(texts),
               extra=str(len(r.get("vectors") or [])))
            ck(results, f"{why}: vectors ALIGNED to inputs (no silent mismap)", _aligned(r.get("vectors") or []),
               extra=str(r.get("vectors")))
    finally:
        adapters._oai_compat_client = _real

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_embed_index_none_providers: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
