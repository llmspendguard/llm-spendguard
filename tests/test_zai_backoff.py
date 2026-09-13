"""Guard — zai_exec retries HTTP 429 with backoff (the plan's dynamic burst/concurrency cap) before giving up.

The z.ai Max coding plan caps concurrency dynamically (measured ~12, 429 beyond). A burst 429 is transient, so a
hard miss that spills every over-cap call to the metered API is wrong. Pins:
  · a 429 → 429 → ok sequence SUCCEEDS (retried), not a hard error;
  · 429 forever → error only after _ZAI_MAX_429_RETRIES+1 attempts (bounded, never infinite);
  · a NON-429 error (500) fails FAST — no rate-limit retry (only 429 is treated as transient);
  · 429 is read from the STRUCTURED HTTPError.code, not error prose.
Offline: urllib is stubbed — no network, no spend; backoff sleeps are zeroed so it runs instantly."""
import os, sys, tempfile, json, io

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-zaibk-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

import urllib.request
import urllib.error
from spendguard import zai_exec

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

zai_exec._key = lambda: "k"                          # a key is present
zai_exec._ZAI_BACKOFF_BASE_S = 0.0                   # zero the backoff so the test runs instantly
zai_exec._ZAI_BACKOFF_JITTER_S = 0.0


class _OkResp:                                        # a context-manager response with the GLM answer
    def read(self): return json.dumps(
        {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 2}}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _urlopen_seq(seq):
    """A fake urlopen that walks `seq` (each item 'ok' | an HTTP code) — the LAST item repeats for extra calls."""
    calls = {"n": 0}
    def _f(req, *a, **k):
        calls["n"] += 1
        item = seq[min(calls["n"] - 1, len(seq) - 1)]
        if item == "ok":
            return _OkResp()
        raise urllib.error.HTTPError("http://x", int(item), "err", {}, io.BytesIO(b'{"error":"rate"}'))
    return _f, calls


print("-- 429 → 429 → ok: retried with backoff, SUCCEEDS (no hard miss) --")
uf, calls = _urlopen_seq(["429", "429", "ok"])
urllib.request.urlopen = uf
r = zai_exec.run_prompt("hi", model="glm-5.3")
ck("succeeded after transient 429s", r.get("text") == "ok" and r.get("error") is None)
ck("took exactly 3 attempts (2 retries then success)", calls["n"] == 3)

print("-- 429 forever: bounded — errors only after _ZAI_MAX_429_RETRIES+1 attempts, never infinite --")
uf2, calls2 = _urlopen_seq(["429"])
urllib.request.urlopen = uf2
r2 = zai_exec.run_prompt("hi", model="glm-5.3")
ck("gives up with an error", bool(r2.get("error")) and r2.get("text") is None)
ck("bounded at MAX_429_RETRIES+1 attempts", calls2["n"] == zai_exec._ZAI_MAX_429_RETRIES + 1)

print("-- non-429 (500) fails FAST: only 429 is treated as transient --")
uf3, calls3 = _urlopen_seq(["500"])
urllib.request.urlopen = uf3
r3 = zai_exec.run_prompt("hi", model="glm-5.3")      # no reasoning → no thinking → no strip-retry either
ck("a 500 is not retried (1 attempt)", bool(r3.get("error")) and calls3["n"] == 1)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_zai_backoff: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
