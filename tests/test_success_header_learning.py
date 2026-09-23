"""Guard — SUCCESS-HEADER learning end-to-end through _call_once: the vendor's rate-limit LIMIT rides x-ratelimit-limit-*
on EVERY successful response (not only the 429), so reading it off a success teaches the governor the real ceiling and
a vendor is paced BEFORE it ever 429s. The parsed completion must be returned UNCHANGED (with_raw_response → .parse()),
so there is no usage/cost regression; a client/endpoint WITHOUT with_raw_response transparently falls back to plain
create and simply learns nothing. Offline: a fake OpenAI client whose with_raw_response.create returns a raw wrapper."""
import os
import sys
import tempfile
import types

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-succhdr-")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"

import openai  # noqa: E402
from spendguard import adapters, config, dispatch, vendor_call  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_COMPLETION = types.SimpleNamespace(
    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
    usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))


class _RawResp:
    """The with_raw_response wrapper the OpenAI SDK returns: rate-limit headers + a .parse() to the parsed completion."""
    headers = {"x-ratelimit-limit-tokens": "2000000", "x-ratelimit-limit-requests": "10000"}

    def parse(self):
        return _COMPLETION


class _RawClient:
    """A client whose chat.completions exposes BOTH plain create AND with_raw_response.create (→ a _RawResp)."""
    def __init__(self, **_kw):
        pass

    def _raw_create(self, **_kw):
        return _RawResp()

    @property
    def chat(self):
        wr = types.SimpleNamespace(create=self._raw_create)
        return types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **k: _COMPLETION, with_raw_response=wr))

    def close(self):
        pass


class _PlainClient:
    """A client with NO with_raw_response (an older SDK / a compat endpoint) — must fall back to plain create."""
    def __init__(self, **_kw):
        pass

    def _create(self, **_kw):
        return _COMPLETION

    @property
    def chat(self):
        return types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def close(self):
        pass


config.api_key = lambda name: "sk-fake"                    # truthy key so _call_once reaches the client
vendor_call.served_check = lambda prov, raw: "unchecked"   # skip the live-catalog preflight (no network)

# ── (1) with_raw_response present → completion returned unchanged AND the limit learned from success headers ──
print("-- (1) _call_once learns the limit from a SUCCESS response and returns the parsed completion unchanged --")
openai.OpenAI = lambda **kw: _RawClient()
for _tmo in (30, None):                                    # BOTH _finish_create call sites: the worker path and no-timeout
    r = adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, timeout_s=_tmo)
    ck(f"parsed completion returned unchanged (.parse() works, timeout_s={_tmo})",
       r.get("text") == "ok" and r.get("in_tok") == 5 and r.get("out_tok") == 3 and not r.get("error"))
ll = dispatch.learned_limits("openai")
ck("the vendor limit was learned from the SUCCESS headers (source='success-header', no 429 needed)",
   ll.get("tpm") == 2000000 and ll.get("rpm") == 10000 and ll.get("source") == "success-header")

# ── (2) no with_raw_response → transparent fallback: the call still works, nothing learned ──
print("-- (2) a client without with_raw_response falls back to plain create (no regression, learns nothing) --")
openai.OpenAI = lambda **kw: _PlainClient()
r2 = adapters._call_once("moonshot:kimi-k3", "hi", max_tokens=100, timeout_s=30)
ck("fallback call succeeds (plain create, parsed completion)", r2.get("text") == "ok" and not r2.get("error"))
ck("no limit learned for a headerless/plain client (moonshot untouched)", not dispatch.learned_limits("moonshot"))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_success_header_learning: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
