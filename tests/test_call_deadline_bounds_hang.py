"""Guard — a hung/slow OpenAI-compat provider call is BOUNDED at wall-clock timeout_s (the kimi-k3 / glm wedge).

The SDK's httpx timeout is per-read, so a long high-reasoning body that trickles evades it and blocks the caller
indefinitely. adapters._call_once now runs each completion through a wall-clock-bounded worker (_bounded_create):
on timeout it closes the client (cancelling the request) and returns a clean deadline error instead of hanging.
Pins:
  · a create() that sleeps far past timeout_s → _call_once RETURNS within ~timeout_s (not the full sleep), with an
    error, and the client was CLOSED (request cancelled → billing stops)
  · a fast create() → returns normally with EXACT usage (no streaming, no token-count guess, no cost regression)
"""
import os, sys, tempfile, time, types
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

import openai
from spendguard import adapters, config, vendor_call

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


_DELAY = {"v": 0.0}
_CLOSED = {"v": False}


class _FakeClient:
    """Stands in for the OpenAI SDK client: its chat.completions.create sleeps `_DELAY` before returning a normal
    completion, and .close() records that the request was cancelled."""
    def __init__(self, **_kw):
        pass

    def _create(self, **_kw):
        time.sleep(_DELAY["v"])
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))

    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def close(self):
        _CLOSED["v"] = True


openai.OpenAI = lambda **kw: _FakeClient()                 # every _call_once client is the fake
config.api_key = lambda name: "sk-fake"                    # a truthy key so _call_once reaches the client
vendor_call.served_check = lambda prov, raw: "unchecked"   # skip the live-catalog preflight (no network in the test)

print("-- a create() that hangs 10s is bounded at timeout_s=1 (returns fast, error, client closed) --")
_DELAY["v"] = 10.0
_CLOSED["v"] = False
t0 = time.time()
r = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, timeout_s=1)
elapsed = time.time() - t0
check("returned within ~timeout_s, not the 10s hang", elapsed < 4.0)
check("returned an error (did not hang, did not fabricate a result)", bool(r.get("error")))
check("the error is a deadline", "deadline" in (r.get("error") or "").lower() or r.get("error_type") == "_CallDeadline")
check("the client was CLOSED (in-flight request cancelled → billing stops)", _CLOSED["v"] is True)
check("no text was invented for the timed-out call", not r.get("text"))

print("-- a fast create() returns normally with EXACT usage (no cost regression) --")
_DELAY["v"] = 0.0
_CLOSED["v"] = False
r2 = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, timeout_s=30)
check("fast call succeeded", r2.get("error") is None and r2.get("text") == "ok")
check("usage is exact (from the completion, not a guess)", r2.get("in_tok") == 5 and r2.get("out_tok") == 3)
check("a fast call did NOT close the client", _CLOSED["v"] is False)

print("-- with no timeout_s, the call is unbounded (existing behaviour, no worker) --")
_DELAY["v"] = 0.0
r3 = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100)
check("no-timeout call still works", r3.get("error") is None and r3.get("text") == "ok")

print("-- the ANTHROPIC streamed path is bounded the same way (every provider call is capped) --")
import anthropic


class _FakeAnthStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        time.sleep(_DELAY["v"])
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")],
            usage=types.SimpleNamespace(input_tokens=5, output_tokens=3), stop_reason="end_turn")


class _FakeAnthropic:
    def __init__(self, **_kw):
        pass

    @property
    def messages(self):
        return types.SimpleNamespace(stream=lambda **kw: _FakeAnthStream())

    def close(self):
        _CLOSED["v"] = True


anthropic.Anthropic = lambda **kw: _FakeAnthropic()
_DELAY["v"] = 10.0
_CLOSED["v"] = False
t0 = time.time()
ra = adapters._call_once("anthropic:claude-opus-4-8", "hi", max_tokens=100, timeout_s=1)
check("anthropic hang bounded within ~timeout_s", (time.time() - t0) < 4.0 and bool(ra.get("error")))
check("anthropic client closed on the deadline", _CLOSED["v"] is True)
_DELAY["v"] = 0.0
_CLOSED["v"] = False
ra2 = adapters._call_once("anthropic:claude-opus-4-8", "hi", max_tokens=100, timeout_s=30)
check("fast anthropic call succeeds with exact usage", ra2.get("error") is None and ra2.get("text") == "ok"
      and ra2.get("in_tok") == 5 and ra2.get("out_tok") == 3)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_call_deadline_bounds_hang: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
