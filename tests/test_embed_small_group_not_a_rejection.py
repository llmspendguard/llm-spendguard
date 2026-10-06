"""embed() must not read a fully-successful SMALL group as a provider rejection.

Guards the fix for the false "batches of N were rejected; the workable size here was 1" warning: a 1-input query
embed, or any partial final chunk, succeeds WHOLE at w < _n and is NOT a rejection. A real rejection is w < len(grp)
(the group had to bisect). No live calls — the OpenAI-compat client is faked; offline, carries its own fake key.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-offline-embed")  # offline tests carry their own fake key

from spendguard import adapters, config  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


class _FakeEmbItem:
    def __init__(self, index, embedding):
        self.index, self.embedding = index, embedding


class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeEmbeddings:
    """Succeeds for any request up to `cap` inputs; raises (like a 400) above it — so the ONLY way a sub-group size
    comes back smaller than the one attempted is a genuine rejection + bisection."""
    def __init__(self, cap):
        self.cap, self.sizes = cap, []

    def create(self, model, input, **kw):
        self.sizes.append(len(input))
        if self.cap is not None and len(input) > self.cap:
            raise RuntimeError(f"400: requested {len(input)} inputs exceeds the cap {self.cap}")
        return _FakeResp([_FakeEmbItem(i, [0.01, 0.02, 0.03]) for i in range(len(input))])


class _FakeClient:
    def __init__(self, cap):
        self.embeddings = _FakeEmbeddings(cap)


_orig_client = adapters._oai_compat_client
_orig_warn = config.warn_once
_warnings = []
try:
    config.warn_once = lambda msg: _warnings.append(msg)
    adapters._oai_compat_client = lambda prov, timeout_s: _FakeClient(2)   # cap=2: >2 inputs is a real 400

    model = "text-embedding-3-small"      # resolves to openai; catalog ceiling 2048

    print("-- a 1-input embed (query) succeeds whole and is NOT reported as a rejection --")
    _warnings.clear()
    r1 = adapters.embed(["just one query"], model=model, max_batch=2)
    check("1-input embed returns its vector",
          r1["error"] is None and r1["vectors"] and r1["vectors"][0] is not None, f"result={r1}")
    check("1-input embed emits NO 'were rejected' warning",
          not any("were rejected" in w for w in _warnings), f"spurious warning(s): {_warnings}")

    print("-- a partial FINAL chunk (< batch size) that succeeds is NOT a rejection --")
    _warnings.clear()
    with tempfile.TemporaryDirectory() as d:
        r2 = adapters.embed(["a", "b", "c"], model=model, max_batch=2,
                            checkpoint=os.path.join(d, "ck.jsonl"))       # chunks [a,b] then [c]
    check("3 inputs at batch=2 all embed",
          r2["error"] is None and sum(v is not None for v in r2["vectors"]) == 3, f"result={r2}")
    check("partial final chunk emits NO 'were rejected' warning",
          not any("were rejected" in w for w in _warnings), f"spurious warning(s): {_warnings}")

    print("-- a GENUINE rejection (group larger than the provider cap) IS still detected --")
    _warnings.clear()
    with tempfile.TemporaryDirectory() as d:
        r3 = adapters.embed(["a", "b", "c", "d"], model=model, max_batch=4,
                            checkpoint=os.path.join(d, "ck.jsonl"))       # 4 > cap 2 → bisect to 2
    check("4 inputs still all embed after bisection",
          r3["error"] is None and sum(v is not None for v in r3["vectors"]) == 4, f"result={r3}")
    check("a real rejection DOES warn (regression guard)",
          any("were rejected" in w for w in _warnings), "a genuine over-cap batch must still warn + shrink")
finally:
    adapters._oai_compat_client = _orig_client
    config.warn_once = _orig_warn

print("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails))
sys.exit(1 if _fails else 0)
