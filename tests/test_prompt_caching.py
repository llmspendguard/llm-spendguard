"""Guard — the adapter PROMPT-CACHES the long, stable system prompt and ACCOUNTS for cache usage on both providers.

The win: a long, identical *_SYSTEM (honestreview's write-time judges) is sent every call; caching it bills the write
once and reads it at a discount forever after. Pins:
  · _cacheable_system: long system (≥ the cache minimum) → cache it; short/empty → don't (a real-quantity bound);
  · ANTHROPIC: a long system is sent as a cache_control block; a short one as a plain string; cache_read /
    cache_write tokens are captured (input_tokens EXCLUDES cache-read, so total in_tok = fresh + read), and the
    cost is priced WITH the cache tokens (read at the discount, write at CACHE_WRITE_5M_MULTIPLIER);
  · FAIL-OPEN: if the cached-system call raises, it retries once with the plain string — a caching hiccup never
    breaks the call, and a deadline still HALTS (no retry);
  · OPENAI: cached_tokens (prompt_tokens_details) is captured; prompt_tokens already INCLUDES it (no double add).
Offline: the SDK clients are stubbed — no network, no spend."""
import os, sys, tempfile, types, json
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cache-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

import anthropic
import openai
from spendguard import adapters, config, vendor_call, pricing

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

config.api_key = lambda name: "sk-fake"
vendor_call.served_check = lambda prov, raw: "unchecked"

LONG_SYS = "You are a rigorous judge. " * 400        # comfortably over the ~1024-token cache minimum
SHORT_SYS = "Be brief."

print("-- _cacheable_system: a real-quantity token bound --")
ck("a long system is cacheable", adapters._cacheable_system(LONG_SYS))
ck("a short system is NOT cacheable", not adapters._cacheable_system(SHORT_SYS))
ck("empty is NOT cacheable", not adapters._cacheable_system(""))

# ── anthropic stub: captures the kw handed to messages.stream, returns usage with cache counts ──
_seen_sys = {"v": None}
_anth_fail_on_list = {"v": False}


class _FakeAnthStream:
    def __init__(self, kw): self._kw = kw
    def __enter__(self):
        _seen_sys["v"] = self._kw.get("system")
        if _anth_fail_on_list["v"] and isinstance(self._kw.get("system"), list):
            raise RuntimeError("cache_control not supported (simulated SDK reject)")
        return self
    def __exit__(self, *a): return False
    def get_final_message(self):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=20, output_tokens=8,
                                        cache_read_input_tokens=900, cache_creation_input_tokens=1100))


class _FakeAnthropic:
    def __init__(self, **_kw): pass
    @property
    def messages(self):
        return types.SimpleNamespace(stream=lambda **kw: _FakeAnthStream(kw))
    def close(self): pass


anthropic.Anthropic = lambda **kw: _FakeAnthropic()

print("-- ANTHROPIC: long system → cached block, cache tokens captured + priced --")
r = adapters._call_once("anthropic:claude-opus-4-8", "judge this", system=LONG_SYS, max_tokens=100)
ck("a long system was sent as a cache_control block",
   isinstance(_seen_sys["v"], list) and _seen_sys["v"][0].get("cache_control", {}).get("type") == "ephemeral")
ck("cache_read captured", r.get("cache_read_tok") == 900)
ck("cache_write captured", r.get("cache_write_tok") == 1100)
ck("in_tok = fresh input + cache_read (anthropic input_tokens excludes cache-read)", r.get("in_tok") == 20 + 900)
_expect = pricing.realtime_cost("claude-opus-4-8", 920, 8, cached_in_tok=900, cache_creation_tok=1100)
ck("cost priced WITH cache tokens (read discount + write multiplier)", abs((r.get("cost") or 0) - _expect) < 1e-9)

print("-- ANTHROPIC: short system → plain string (below the cache minimum) --")
adapters._call_once("anthropic:claude-opus-4-8", "hi", system=SHORT_SYS, max_tokens=100)
ck("a short system was sent as a plain string", isinstance(_seen_sys["v"], str))

print("-- FAIL-OPEN: a cached-system reject retries once with the plain string --")
_anth_fail_on_list["v"] = True
r2 = adapters._call_once("anthropic:claude-opus-4-8", "judge this", system=LONG_SYS, max_tokens=100)
ck("the call still SUCCEEDED (fell back to plain)", r2.get("error") is None and r2.get("text") == "ok")
ck("the retry used a plain-string system", isinstance(_seen_sys["v"], str))
_anth_fail_on_list["v"] = False

# ── openai stub: usage with prompt_tokens_details.cached_tokens ──
class _FakeOAClient:
    def __init__(self, **_kw): pass
    def _create(self, **_kw):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=1500, completion_tokens=10,
                                        prompt_tokens_details=types.SimpleNamespace(cached_tokens=1200)))
    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
    def close(self): pass


openai.OpenAI = lambda **kw: _FakeOAClient()

print("-- OPENAI: cached_tokens captured; prompt_tokens already includes it (no double add) --")
ro = adapters._call_once("openai:gpt-5.5", "hi", system=LONG_SYS, max_tokens=100)
ck("cached_tokens captured as cache_read", ro.get("cache_read_tok") == 1200)
ck("no separate write charge on openai", ro.get("cache_write_tok") == 0)
ck("in_tok stays prompt_tokens (already includes cached — not re-added)", ro.get("in_tok") == 1500)

print("-- the gate BOOKS the prompt-cache saving into the guarded-savings ledger (visible in receipt/savings/MCP) --")
from spendguard import gate, guard, receipt
gate._record_rt("gpt-5.5", {}, 1000, 10, cached=900, cache_creation=0)     # 900 cache-read tok on a priced model
_by = (guard.saved_since().get("by_source") or {})
ck("a cached call books a 'prompt_cache' saving (read discount, measured)", (_by.get("prompt_cache") or 0) > 0)
ck("'prompt_cache' is CERTAIN (measured), not counterfactual", "prompt_cache" in guard.CERTAIN)
ck("it counts in the certain total, never in real-$ or est-value", guard.saved_since().get("certain", 0) >= (_by.get("prompt_cache") or 0))
_lines = receipt._saved_lines({"est_value": {"month": 0}, "real_month": 0, "api": {"month": 0}})
ck("the receipt NAMES the prompt-cache saving", any("prompt-cache" in ln for ln in _lines))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_prompt_caching: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
