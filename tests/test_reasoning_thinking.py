"""Guard — reasoning engages Anthropic-shape extended THINKING from a MEASURED budget, safely.

Anthropic thinking is a token BUDGET (not an effort ordinal) and CONFLICTS with a forced-tool schema. Pins:
  · models.thinking_budget: None for minimal/none/no-fact; a measured `thinking:<ordinal>` fact → its budget;
    trimmed to fit max_tokens (reserve output room); None when it cannot reach the API minimum; `thinking:*` fallback;
  · anthropic metered: reasoning + a fact + NO schema → a thinking block on the request; reasoning + schema →
    NO thinking (the conflict) + a one-time notice; a rejection of thinking FAILS OPEN (retry once without it);
  · zai_exec lane: a fact → a thinking block on the Anthropic-shape body; if the enriched call fails it retries
    ONCE without thinking (no error-prose classification) so the lane never breaks.
Offline: the SDK + HTTP are stubbed — no network, no spend."""
import os, sys, tempfile, types, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-think-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import models, adapters, config, vendor_call

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

print("-- models.thinking_budget: measured fact, clamped to API constraints, never guessed --")
ck("no fact → None (honest default until measured)", models.thinking_budget("claude-opus-4-8", "high", 20000) is None)
models.add_fact("claude-opus-4-8", "thinking:high", 8000, source="experiment")
ck("a measured fact → its budget", models.thinking_budget("claude-opus-4-8", "high", 20000) == 8000)
ck("reasoning 'minimal' → None (no thinking)", models.thinking_budget("claude-opus-4-8", "minimal", 20000) is None)
ck("reasoning '' → None", models.thinking_budget("claude-opus-4-8", "", 20000) is None)
ck("a big fact is TRIMMED to fit max_tokens (reserve output room)",
   models.thinking_budget("claude-opus-4-8", "high", 4000) == 4000 - 512)
ck("cannot reach the API minimum → None", models.thinking_budget("claude-opus-4-8", "high", 1200) is None)
models.add_fact("claude-haiku-4-5", "thinking:*", 2000, source="experiment")
ck("thinking:* fallback applies to any ordinal", models.thinking_budget("claude-haiku-4-5", "medium", 20000) == 2000)

# ── stub the SDKs (offline) ──
config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"

import anthropic
_seen = {"kw": None}
_reject_thinking = {"v": False}


class _FakeStream:
    def __init__(self, kw): self._kw = kw
    def __enter__(self):
        _seen["kw"] = self._kw
        if _reject_thinking["v"] and "thinking" in self._kw:
            raise RuntimeError("thinking not supported here (simulated reject)")
        return self
    def __exit__(self, *a): return False
    def get_final_message(self):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                        cache_read_input_tokens=0, cache_creation_input_tokens=0))


class _FakeAnth:
    def __init__(self, **_k): pass
    @property
    def messages(self):
        return types.SimpleNamespace(stream=lambda **kw: _FakeStream(kw))
    def close(self): pass


anthropic.Anthropic = lambda **kw: _FakeAnth()

print("-- anthropic: reasoning + fact + NO schema → a thinking block on the request --")
adapters._call_once("anthropic:claude-opus-4-8", "solve this", reasoning="high", max_tokens=20000)
ck("a thinking block was sent, budget from the fact",
   isinstance(_seen["kw"].get("thinking"), dict) and _seen["kw"]["thinking"].get("budget_tokens") == 8000)

print("-- anthropic: reasoning + schema → NO thinking (the forced-tool conflict) --")
_seen["kw"] = None
adapters._call_once("anthropic:claude-opus-4-8", "solve this", reasoning="high", max_tokens=20000,
                    schema={"type": "object", "properties": {"x": {"type": "string"}}})
ck("no thinking block when a schema is forced", "thinking" not in (_seen["kw"] or {}))

print("-- anthropic: a thinking rejection FAILS OPEN (retry once without it) --")
_reject_thinking["v"] = True
r = adapters._call_once("anthropic:claude-opus-4-8", "solve this", reasoning="high", max_tokens=20000)
ck("the call still SUCCEEDED (stripped thinking, retried)", r.get("error") is None and r.get("text") == "ok")
ck("the retry carried NO thinking block", "thinking" not in (_seen["kw"] or {}))
_reject_thinking["v"] = False

print("-- zai_exec lane: a fact → a thinking block; a failure retries ONCE without thinking --")
from spendguard import zai_exec
import urllib.request
models.add_fact("glm-5.3", "thinking:high", 6000, source="experiment")
zai_exec._key = lambda: "zkey"
_posts = []
_fail_first_on_thinking = {"v": False}


class _FakeResp:                                   # a context manager — zai_exec now closes the response via `with`
    def read(self): return json.dumps(
        {"content": [{"type": "thinking", "text": "…"}, {"type": "text", "text": "answer"}],
         "usage": {"input_tokens": 12, "output_tokens": 7}}).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_urlopen(req, *a, **k):
    body = json.loads(req.data.decode("utf-8"))
    _posts.append(body)
    if _fail_first_on_thinking["v"] and "thinking" in body:
        raise RuntimeError("z.ai: thinking rejected (simulated)")
    return _FakeResp()


urllib.request.urlopen = _fake_urlopen

_posts.clear()
out = zai_exec.run_prompt("do it", model="glm-5.3", reasoning="high")
ck("zai body carried the thinking block (budget from the fact)",
   _posts and _posts[0].get("thinking", {}).get("budget_tokens") == 6000)
ck("text extraction kept only the text block (thinking ignored)", out.get("text") == "answer" and out.get("error") is None)

_posts.clear()
_fail_first_on_thinking["v"] = True
out2 = zai_exec.run_prompt("do it", model="glm-5.3", reasoning="high")
ck("a thinking rejection retried ONCE without thinking", len(_posts) == 2 and "thinking" not in _posts[1])
ck("the plain retry SUCCEEDED (lane never breaks)", out2.get("text") == "answer" and out2.get("error") is None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reasoning_thinking: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
