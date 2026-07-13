"""Raw-HTTP capture + per-call tool fees — the last silent channels, made visible.
  • a raw httpx/requests call to a provider host (no SDK) records its usage into the same realtime
    ledger; an unparseable provider response is logged as a LOUD unmetered event — never invisible;
  • SDK-originated traffic is SUPPRESSED at the HTTP layer (the _call_orig ContextVar) — no double count;
  • web-search tool invocations (Responses output items / Anthropic server_tool_use) are billed per
    CALL, not per token — counted and recorded as their own fee row (unpriced → $0 + warn).
Offline (httpx MockTransport, stubbed results), zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-http-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import types
from spendguard import gate as spend_gate
from spendguard import http_capture, pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


recorded, logged = [], []
spend_gate._record_rt = lambda model, kw, i, o, **k: recorded.append((model, i, o, k.get("cost"), k.get("provider")))
http_orig_log = spend_gate._log
spend_gate._log = lambda rec: logged.append(rec)

# ── tool fees: Responses web_search_call items + Anthropic server_tool_use, priced per call ──
pricing.UNIT_PRICES["web_search_call"]["gpt-5.5"] = 0.01
resp = types.SimpleNamespace(
    output=[types.SimpleNamespace(type="web_search_call"), types.SimpleNamespace(type="message"),
            types.SimpleNamespace(type="web_search_call")],
    usage=types.SimpleNamespace(server_tool_use=None))
ck("counts Responses web_search_call items (2)", spend_gate._tool_fee_count(resp) == 2)
spend_gate._record_tool_fees("gpt-5.5", {}, resp)
ck("fee recorded as its own row (2 × $0.01)", recorded[-1] == ("gpt-5.5", 0, 0, 0.02, None))

anth = types.SimpleNamespace(output=None,
                             usage=types.SimpleNamespace(server_tool_use=types.SimpleNamespace(web_search_requests=3)))
ck("counts Anthropic server_tool_use.web_search_requests (3)", spend_gate._tool_fee_count(anth) == 3)
spend_gate._record_tool_fees("claude-sonnet-4-6", {}, anth)
ck("unpriced tool fee → $0 row (never silent, never guessed)", recorded[-1] == ("claude-sonnet-4-6", 0, 0, 0.0, None))
n0 = len(recorded)
spend_gate._record_tool_fees("gpt-5.5", {}, types.SimpleNamespace(output=[], usage=None))
ck("no tool calls → no fee row", len(recorded) == n0)

# ── raw-HTTP capture: known usage shape → ledger; junk → unmetered event; SDK traffic suppressed ──
import httpx

CHAT = {"model": "gpt-5.5", "usage": {"prompt_tokens": 120, "completion_tokens": 30}}
ANTH = {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 50, "output_tokens": 9}}


def handler(request):
    p = request.url.path
    if p == "/v1/chat/completions":
        return httpx.Response(200, json=CHAT)
    if p == "/v1/messages":
        return httpx.Response(200, json=ANTH)
    return httpx.Response(200, content=b"event: stream\n\n")   # unparseable → unmetered


ck("http_capture.install() wires httpx + requests", http_capture.install() is True)
ck("httpx.Client.send is patched", getattr(httpx.Client.send, "_spend_gated", False))
import requests as _rq
ck("requests.Session.send is patched", getattr(_rq.Session.send, "_spend_gated", False))

client = httpx.Client(transport=httpx.MockTransport(handler))
client.get("https://api.openai.com/v1/chat/completions")
ck("raw OpenAI-shaped response recorded (120/30, provider=openai)",
   recorded[-1] == ("gpt-5.5", 120, 30, None, "openai"))
client.get("https://api.anthropic.com/v1/messages")
ck("raw Anthropic-shaped response recorded (50/9, provider=anthropic)",
   recorded[-1] == ("claude-sonnet-4-6", 50, 9, None, "anthropic"))

n0, l0 = len(recorded), len(logged)
client.get("https://api.openai.com/v1/some/other/endpoint")
ck("unparseable provider response → raw_http_unmetered event, no fake tokens",
   len(recorded) == n0 and len(logged) == l0 + 1 and logged[-1]["kind"] == "raw_http_unmetered")

n0 = len(recorded) + len(logged)
client.get("https://example.com/v1/chat/completions")
ck("non-provider hosts are never touched", len(recorded) + len(logged) == n0)

tok = http_capture.in_sdk_call.set(True)
client.get("https://api.openai.com/v1/chat/completions")
http_capture.in_sdk_call.reset(tok)
ck("SDK-originated traffic is SUPPRESSED (no double count)", len(recorded) + len(logged) == n0)

os.environ["SPENDGUARD_HTTP_CAPTURE"] = "off"
client.get("https://api.openai.com/v1/chat/completions")
ck("knob: SPENDGUARD_HTTP_CAPTURE=off disables the layer", len(recorded) + len(logged) == n0)
del os.environ["SPENDGUARD_HTTP_CAPTURE"]

# ── the gate's SDK wrappers actually set the suppression flag around the real call ──
seen_flag = []
orig_probe = spend_gate._record_rt
probe_orig = lambda self, *a, **kw: seen_flag.append(http_capture.in_sdk_call.get()) or types.SimpleNamespace(  # noqa: E731
    model="gpt-5.5", usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))
from openai.resources.chat import completions as _oc
_oc.Completions.create = probe_orig
spend_gate.install()
import openai
oc = openai.OpenAI(api_key="test-key-not-real")
os.environ["GATE_RT_BUDGET"] = "1000"
oc.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}], max_tokens=10)
ck("inside a gated SDK call, in_sdk_call is TRUE (raw layer stands down)", seen_flag == [True])
spend_gate._record_rt = orig_probe

print(("[OK]" if not fails else "[FAIL]") + " http capture + tool fees: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
