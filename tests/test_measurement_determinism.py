"""The measurement-only deterministic path + its STANDARDIZED cross-provider control surface.

temperature/top_p/seed are honored ONLY when measurement=True, forced onto the metered API (a $0 lane CLI has no such
channel), and mapped through ONE capability SSOT (adapters.generation_support) onto what each vendor/model actually
honors — so the SAME adapters.call(..., measurement=True, temperature=0, seed=…) is correct for OpenAI, a reasoning
model, anthropic and the compat vendors, and a knob a vendor cannot take is RECORDED as dropped (never a silent 400,
never silently ignored). Refused on a production call. This is what lets a priced A/B be a single deterministic pair
instead of n replicates to separate the effect from run noise (measured: a 24-pt nondeterminism band vs a 5.6-pt
signal). Offline — the SDK create/stream is stubbed, no network, zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-measure-det-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


# ── 0. the capability SSOT maps the ONE surface onto each vendor correctly (standardized) ──
ck("non-reasoning OpenAI honors all three", adapters.generation_support("openai", "gpt-4.1-mini") == {"temperature", "top_p", "seed"})
ck("reasoning OpenAI honors seed only (sampling knobs rejected)", adapters.generation_support("openai", "gpt-5.5") == {"seed"})
ck("anthropic honors temperature/top_p, no seed", adapters.generation_support("anthropic", "claude-opus-4-8") == {"temperature", "top_p"})
ck("a compat vendor honors temperature/top_p, no seed", adapters.generation_support("gemini", "gemini-3.7-flash") == {"temperature", "top_p"})


# ── 1. PRODUCTION refusal: a generation knob without measurement=True raises (governance unchanged) ──
for knob in ("temperature", "top_p", "seed"):
    try:
        adapters.call("gpt-4.1-mini", "hi", **{knob: 0 if knob != "seed" else 7})
        ck(f"{knob} on a production call raises TypeError", False)
    except TypeError as e:
        ck(f"{knob} on a production call raises TypeError", "measurement" in str(e).lower())


# ── openai stub that CAPTURES the request kwargs (no with_raw_response → plain create(**cw)) ──
_captured = {}
class _OAUsage:   prompt_tokens = 1000; completion_tokens = 200
class _OAMsg:     content = "stub openai reply"
class _OAChoice:  message = _OAMsg()
class _OAResp:    choices = [_OAChoice()]; usage = _OAUsage()
class _FakeCompletions:
    def create(self, *a, **k):
        _captured.clear(); _captured.update(k)
        return _OAResp()
class _FakeChat:   completions = _FakeCompletions()
class _FakeOpenAI:
    def __init__(self, *a, **k):  self.chat = _FakeChat()

os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
import openai as _openai_mod  # noqa: E402
_orig_openai = _openai_mod.OpenAI

# ── 2. measurement + all three knobs on a NON-reasoning OpenAI model: metered forced, all honored + stamped ──
_openai_mod.OpenAI = _FakeOpenAI
try:
    r = adapters.call("gpt-4.1-mini", "hello", intent="onlyhome-bakeoff", measurement=True,
                      temperature=0, top_p=1, seed=20261008)
finally:
    _openai_mod.OpenAI = _orig_openai
ck("measurement call succeeded on the stubbed metered path", r.get("text") == "stub openai reply" and not r.get("error"))
ck("measurement determinism forced the METERED API (executor='api', not a $0 lane)", r.get("executor") == "api")
ck("temperature reached the request", _captured.get("temperature") == 0)
ck("top_p reached the request", _captured.get("top_p") == 1)
ck("seed reached the request", _captured.get("seed") == 20261008)
ck("all three stamped as gen_params_applied", r.get("gen_params_applied") == {"temperature": 0, "top_p": 1, "seed": 20261008})
ck("nothing dropped for a non-reasoning OpenAI model", not r.get("gen_params_dropped"))

# ── 2b. a REASONING OpenAI model drops the sampling knobs, keeps seed — recorded, not a silent 400 ──
_captured.clear()
_openai_mod.OpenAI = _FakeOpenAI
try:
    r2 = adapters.call("gpt-5.5", "hello", intent="bakeoff", measurement=True, temperature=0, seed=123)
finally:
    _openai_mod.OpenAI = _orig_openai
ck("reasoning model: seed reached the request", _captured.get("seed") == 123)
ck("reasoning model: temperature was NOT sent (would 400)", "temperature" not in _captured)
ck("reasoning model: temperature recorded as dropped", r2.get("gen_params_dropped") == {"temperature": 0})
ck("reasoning model: seed recorded as applied", r2.get("gen_params_applied") == {"seed": 123})

# ── 3. measurement with NO knobs alters nothing (the gate only fires on a knob) ──
_captured.clear()
_openai_mod.OpenAI = _FakeOpenAI
try:
    r3 = adapters.call("gpt-4.1-mini", "hello", intent="x", measurement=True, metered_only=True)
finally:
    _openai_mod.OpenAI = _orig_openai
ck("a measurement call with no knobs sends no temperature/seed", "temperature" not in _captured and "seed" not in _captured)
ck("a measurement call with no knobs has no gen_params on the result", not r3.get("gen_params"))


# ── 4. anthropic: temperature applied, seed dropped (no anthropic seed param) — same standardized surface ──
_acap = {}
class _AntUsage:  input_tokens = 500; output_tokens = 50; cache_read_input_tokens = 0; cache_creation_input_tokens = 0
class _AntBlock:
    type = "text"
    text = "stub claude reply"
class _AntMsg:
    content = [_AntBlock()]; usage = _AntUsage(); stop_reason = "end_turn"
class _FakeStream:
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def get_final_message(self): return _AntMsg()
class _FakeMessages:
    def stream(self, *a, **k):
        _acap.clear(); _acap.update(k)
        return _FakeStream()
    def create(self, *a, **k):
        raise AssertionError("anthropic path must stream")
class _FakeAnthropic:
    def __init__(self, *a, **k): self.messages = _FakeMessages()

import anthropic as _anthropic_mod  # noqa: E402
_orig_anthropic = _anthropic_mod.Anthropic
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-real"
_anthropic_mod.Anthropic = _FakeAnthropic
try:
    ra = adapters.call("claude-opus-4-8", "hi", intent="bakeoff", measurement=True, temperature=0, seed=20261008)
finally:
    _anthropic_mod.Anthropic = _orig_anthropic
ck("anthropic measurement call succeeded", ra.get("text") == "stub claude reply" and not ra.get("error"))
ck("temperature reached the anthropic request", _acap.get("temperature") == 0)
ck("seed was NOT sent to anthropic (no seed param)", "seed" not in _acap)
ck("anthropic seed recorded as dropped", ra.get("gen_params_dropped") == {"seed": 20261008})

print(f"\n{'[FAIL]' if fails else 'OK'} test_measurement_determinism: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
