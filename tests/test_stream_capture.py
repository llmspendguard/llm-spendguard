"""Streaming usage capture — the gate wraps a streaming response in a transparent proxy that passes chunks through,
captures ACTUAL usage as it streams, and records it on exhaustion (replacing the input+max_tokens estimate). High
blast radius (a wrapper bug breaks streaming everywhere), so this exercises all 4 combos — sync/async × OpenAI/
Anthropic — asserting: chunks pass through untouched · actual usage is captured (not estimated) · the Stream's other
attrs still delegate · OpenAI chat gets include_usage injected (Anthropic does not). Offline, mocked, zero spend."""
import os, sys, tempfile, asyncio

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-stream-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


class O:                                              # generic attr bag for mock chunks/usage
    def __init__(self, **k):
        self.__dict__.update(k)


# capture what the gate records (no ledger I/O)
REC = []
gate._record_rt = lambda model, kw, in_tok, out_tok, cached=0, latency=None, output=None, finish=None: \
    REC.append({"model": model, "in": in_tok, "out": out_tok, "finish": finish})


class MockStream:                                     # an iterable stream with extra attrs (to test delegation)
    def __init__(self, chunks):
        self._chunks = chunks
        self.response = "HTTP_RESPONSE"
        self.closed = False
    def __iter__(self):
        return iter(self._chunks)
    def close(self):
        self.closed = True


class AsyncMockStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.response = "HTTP_RESPONSE"
    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


# ── OpenAI chat: usage on the FINAL chunk (only emitted with stream_options include_usage) ──
def oai_chunks():
    return [O(choices=[O(delta=O(content="hi"), finish_reason=None)], usage=None),
            O(choices=[O(delta=O(content="!"), finish_reason="stop")], usage=None),
            O(choices=[], usage=O(prompt_tokens=100, completion_tokens=42))]   # final usage chunk

# ── Anthropic: input usage in message_start, output in message_delta (+ stop_reason) ──
def anth_chunks():
    return [O(type="message_start", message=O(usage=O(input_tokens=200, output_tokens=1))),
            O(type="content_block_delta", delta=O(text="hi")),
            O(type="message_delta", usage=O(output_tokens=55), delta=O(stop_reason="end_turn"))]

KW_OAI = lambda: {"model": "gpt-5.5", "messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 500}
KW_ANT = lambda: {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 500}

# orig sees the kw AFTER the wrapper injects into it — that's the dict that reaches the real API, so capture it there
# (calling w(None, **kw) re-collects kwargs into a fresh inner dict, so the outer `kw` never sees the injection).
def mk_orig(chunks, seen, async_=False):
    if async_:
        async def o(self, *a, **k):
            seen.clear(); seen.update(k); return AsyncMockStream(chunks)
        return o
    def o(self, *a, **k):
        seen.clear(); seen.update(k); return MockStream(chunks)
    return o

# ── 1. sync OpenAI chat ──
REC.clear(); seen = {}
w = gate._wrap_rt(mk_orig(oai_chunks(), seen), gate._est_oai_chat, gate._act_oai_chat, False)
proxy = w(None, **KW_OAI())
ck("openai chat: include_usage injected into the call that reaches the API", seen.get("stream_options", {}).get("include_usage") is True)
ck("proxy delegates non-iter attrs (.response)", proxy.response == "HTTP_RESPONSE")
chunks = list(proxy)
ck("sync openai: all chunks pass through", len(chunks) == 3)
ck("sync openai: ACTUAL usage captured (100/42, not the 500 ceiling)", REC and REC[-1]["in"] == 100 and REC[-1]["out"] == 42)
ck("sync openai: finish captured", REC[-1]["finish"] == "stop")

# ── 2. sync Anthropic (no injection; usage native) ──
REC.clear(); seen = {}
w = gate._wrap_rt(mk_orig(anth_chunks(), seen), gate._est_anth_msg, gate._act_anth_msg, False)
chunks = list(w(None, **KW_ANT()))
ck("anthropic: include_usage NOT injected", "stream_options" not in seen)
ck("sync anthropic: chunks pass through", len(chunks) == 3)
ck("sync anthropic: ACTUAL usage captured (200/55)", REC and REC[-1]["in"] == 200 and REC[-1]["out"] == 55)
ck("sync anthropic: stop_reason captured", REC[-1]["finish"] == "end_turn")

# ── 3 & 4. async OpenAI + Anthropic ──
async def _run_async(wa, kw):
    proxy = await wa(None, **kw)
    return [c async for c in proxy]

REC.clear(); seen = {}
wa = gate._wrap_rt(mk_orig(oai_chunks(), seen, async_=True), gate._est_oai_chat, gate._act_oai_chat, True)
got = asyncio.run(_run_async(wa, KW_OAI()))
ck("async openai: include_usage injected", seen.get("stream_options", {}).get("include_usage") is True)
ck("async openai: chunks pass through", len(got) == 3)
ck("async openai: ACTUAL usage captured (100/42)", REC and REC[-1]["in"] == 100 and REC[-1]["out"] == 42)

REC.clear(); seen = {}
wa = gate._wrap_rt(mk_orig(anth_chunks(), seen, async_=True), gate._est_anth_msg, gate._act_anth_msg, True)
got = asyncio.run(_run_async(wa, KW_ANT()))
ck("async anthropic: chunks pass through", len(got) == 3)
ck("async anthropic: ACTUAL usage captured (200/55)", REC and REC[-1]["in"] == 200 and REC[-1]["out"] == 55)

print(("[OK]" if not fails else "[FAIL]") + " stream-capture: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
