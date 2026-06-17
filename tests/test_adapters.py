"""Offline test for the provider adapters — NO network, NO API calls. Stubs the SDK create methods.

Covers:
  * register_provider() / provider_for() — explicit 'provider:model', prefix resolution (builtin +
    a freshly-registered provider), and the unknown-model ValueError.
  * call() — both the openai-compatible and anthropic code paths with the SDK create() STUBBED (no
    network), the no-key short-circuit, the n/a-cost path (model not in price table), and the
    exception-is-captured-never-raised contract.
"""
import os
import sys
import tempfile

# Isolate SPENDGUARD_HOME before the venv sitecustomize loads the gate (re-exec once; the runner sets the flag).
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters                                      # noqa: E402

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# ── provider_for: explicit provider:model ───────────────────────────────────────────────────────────────────
print("-- provider_for: explicit provider:model --")
check("explicit 'qwen:my-model' -> qwen", adapters.provider_for("qwen:my-model") == "qwen")
check("explicit 'anything:foo' wins over prefixes", adapters.provider_for("anything:gpt-5.5") == "anything")

# ── provider_for: prefix resolution against builtins ─────────────────────────────────────────────────────────
print("-- provider_for: builtin prefix resolution --")
check("gpt-5.5 -> openai", adapters.provider_for("gpt-5.5") == "openai")
check("o3-mini -> openai", adapters.provider_for("o3-mini") == "openai")
check("claude-opus-4-8 -> anthropic", adapters.provider_for("claude-opus-4-8") == "anthropic")
check("gemini-2.5-pro -> gemini", adapters.provider_for("gemini-2.5-pro") == "gemini")
check("deepseek-chat -> deepseek", adapters.provider_for("deepseek-chat") == "deepseek")
check("qwq-32b -> qwen", adapters.provider_for("qwq-32b") == "qwen")

# ── provider_for: unknown -> ValueError ──────────────────────────────────────────────────────────────────────
print("-- provider_for: unknown raises --")
try:
    adapters.provider_for("totally-made-up-model")
    check("unknown model raises ValueError", False)
except ValueError:
    check("unknown model raises ValueError", True)

# ── register_provider: add a new openai-compatible provider, then resolve by prefix ──────────────────────────
print("-- register_provider --")
adapters.register_provider("acme", base_url="https://api.acme.example/v1",
                           key_env="ACME_API_KEY", prefixes=["acme-", "ax"])
check("acme registered into PROVIDERS", "acme" in adapters.PROVIDERS)
check("prefixes stored as tuple", isinstance(adapters.PROVIDERS["acme"]["prefixes"], tuple))
check("default kind is openai", adapters.PROVIDERS["acme"]["kind"] == "openai")
check("acme-1 -> acme (newly-registered prefix)", adapters.provider_for("acme-1") == "acme")
adapters.register_provider("myanthro", base_url=None, key_env="MYANTHRO_KEY",
                           prefixes=("myco-",), kind="anthropic")
check("override kind=anthropic honored", adapters.PROVIDERS["myanthro"]["kind"] == "anthropic")

# ── call(): no key -> short-circuits with an error dict, never raises ─────────────────────────────────────────
# config.api_key also walks .env chains (cwd/.env, SPENDGUARD_HOME/.env), which may hold a real key on this
# machine. Force the no-key branch hermetically by stubbing config.api_key -> "" for this one assertion.
print("-- call(): no key short-circuit --")
from spendguard import config                                       # noqa: E402
_orig_api_key = config.api_key
config.api_key = lambda name: ""
try:
    r = adapters.call("gpt-5.5", "hello")
finally:
    config.api_key = _orig_api_key
check("no-key call returns dict (no raise)", isinstance(r, dict))
check("no-key call reports the missing key env", r["error"] and "OPENAI_API_KEY" in r["error"])
check("no-key call has provider+model populated", r["provider"] == "openai" and r["model"] == "gpt-5.5")

# ── call(): openai-compatible path with the SDK STUBBED (no network) ──────────────────────────────────────────
print("-- call(): openai path (stubbed SDK) --")
os.environ["OPENAI_API_KEY"] = "sk-test-not-real"


class _OAUsage:
    prompt_tokens = 1000
    completion_tokens = 200


class _OAMsg:
    content = "stubbed openai reply"


class _OAChoice:
    message = _OAMsg()


class _OAResp:
    choices = [_OAChoice()]
    usage = _OAUsage()


class _FakeCompletions:
    def create(self, *a, **k):
        # assert the adapter built messages + passed the raw model (no network)
        assert k["model"] == "gpt-5.5"
        return _OAResp()


class _FakeChat:
    completions = _FakeCompletions()


class _FakeOpenAI:
    def __init__(self, *a, **k):
        self.chat = _FakeChat()


import openai as _openai_mod                                         # noqa: E402
_orig_openai = _openai_mod.OpenAI
_openai_mod.OpenAI = _FakeOpenAI
try:
    r = adapters.call("gpt-5.5", "hello", system="you are terse")
finally:
    _openai_mod.OpenAI = _orig_openai
check("openai call returns the stubbed text", r["text"] == "stubbed openai reply")
check("openai call captured token usage", r["in_tok"] == 1000 and r["out_tok"] == 200)
check("openai call has no error", r["error"] is None)
check("openai call priced a known model (cost set)", isinstance(r["cost"], float) and r["cost"] > 0)
check("openai call set latency", isinstance(r["latency"], float))

# ── call(): cost n/a path — known provider prefix but model absent from the price table ──────────────────────
print("-- call(): unknown-price model -> cost n/a --")
adapters.register_provider("oai2", base_url=None, key_env="OPENAI_API_KEY", prefixes=("freemodel-",))


class _FakeCompletions2:
    def create(self, *a, **k):
        return _OAResp()


class _FakeChat2:
    completions = _FakeCompletions2()


class _FakeOpenAI2:
    def __init__(self, *a, **k):
        self.chat = _FakeChat2()


_openai_mod.OpenAI = _FakeOpenAI2
try:
    r = adapters.call("freemodel-x", "hi")
finally:
    _openai_mod.OpenAI = _orig_openai
check("unpriced model -> cost is None (shown n/a), still returns text", r["cost"] is None and r["text"] == "stubbed openai reply")

# ── call(): anthropic path with the SDK STUBBED (no network) ──────────────────────────────────────────────────
print("-- call(): anthropic path (stubbed SDK) --")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-real"


class _AntUsage:
    input_tokens = 500
    output_tokens = 50


class _AntBlock:
    type = "text"
    text = "stubbed claude reply"


class _AntMsg:
    content = [_AntBlock()]
    usage = _AntUsage()


class _FakeMessages:
    def create(self, *a, **k):
        assert k["model"] == "claude-opus-4-8"
        assert k["messages"][0]["content"] == "hi"
        return _AntMsg()


class _FakeAnthropic:
    def __init__(self, *a, **k):
        self.messages = _FakeMessages()


import anthropic as _anthropic_mod                                   # noqa: E402
_orig_anthropic = _anthropic_mod.Anthropic
_anthropic_mod.Anthropic = _FakeAnthropic
try:
    r = adapters.call("claude-opus-4-8", "hi", system="be brief")
finally:
    _anthropic_mod.Anthropic = _orig_anthropic
check("anthropic call returns the joined text blocks", r["text"] == "stubbed claude reply")
check("anthropic call captured token usage", r["in_tok"] == 500 and r["out_tok"] == 50)
check("anthropic call priced opus-4-8 (cost set)", isinstance(r["cost"], float) and r["cost"] > 0)
check("anthropic call has no error", r["error"] is None)
# anthropic again WITHOUT system -> exercises the `if system:` false branch (no system kw added)
_anthropic_mod.Anthropic = _FakeAnthropic
try:
    r = adapters.call("claude-opus-4-8", "hi")
finally:
    _anthropic_mod.Anthropic = _orig_anthropic
check("anthropic call w/o system still returns text", r["text"] == "stubbed claude reply" and r["error"] is None)

# ── call(): SDK raises -> error captured, never propagated ────────────────────────────────────────────────────
print("-- call(): SDK exception is captured (never raises) --")


class _BoomCompletions:
    def create(self, *a, **k):
        raise RuntimeError("simulated transport failure (offline)")


class _BoomChat:
    completions = _BoomCompletions()


class _FakeOpenAIBoom:
    def __init__(self, *a, **k):
        self.chat = _BoomChat()


_openai_mod.OpenAI = _FakeOpenAIBoom
try:
    r = adapters.call("gpt-5.5", "hello")
finally:
    _openai_mod.OpenAI = _orig_openai
check("SDK exception -> returns dict (no raise)", isinstance(r, dict))
check("SDK exception -> error string captured", r["error"] and "simulated transport failure" in r["error"])
check("SDK exception -> latency still recorded", isinstance(r["latency"], float))

print(f"\n{'[FAIL]' if failures else 'OK'} adapters: {failures} failure(s)")
sys.exit(1 if failures else 0)
