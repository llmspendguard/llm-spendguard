"""Gemini reasoning crosses two namespaces, and a call must be RESPELLED for whichever one serves it.

agy (the Gemini subscription lane) names reasoning effort as a MODEL-ID SUFFIX — gemini-3.7-flash-medium — and
ignores the reasoning kwarg. The METERED Gemini API names the BARE model (gemini-3.7-flash) and takes effort as a
reasoning PARAMETER. Sending the wrong spelling breaks silently in opposite ways: an agy suffix id handed to the
metered API 404s ("models/gemini-3.7-flash-medium is not found"), and a bare id + reasoning=medium handed to the
lane drops the tier and runs agy's default. This pins BOTH conversions so neither regresses:

  (a) split/compose respell agy's fixed suffix vocabulary (low/medium/high) — and leave non-suffixed ids alone;
  (b) an agy id on the METERED path reaches the SDK as the BARE model + a reasoning_effort (never the 404 id);
  (c) a bare id + reasoning on the AGY LANE reaches run_prompt as the SUFFIXED id (never a dropped tier).

Offline: the OpenAI SDK and the agy lane module are stubbed to capture exactly what each was asked to send. No
network, no real key, no spend.
"""
import os
import sys
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-gemrx-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters                                                        # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- (a) split/compose respell agy's fixed suffix vocabulary, and leave other ids alone --")
ck("split: agy suffix id -> (bare, tier)", adapters._split_gemini_reasoning("gemini-3.7-flash-medium") == ("gemini-3.7-flash", "medium"))
ck("split: -latest alias is NOT a tier suffix", adapters._split_gemini_reasoning("gemini-flash-latest") == ("gemini-flash-latest", None))
ck("split: -lite is NOT a tier suffix", adapters._split_gemini_reasoning("gemini-3.5-flash-lite") == ("gemini-3.5-flash-lite", None))
ck("compose: bare id + tier -> suffixed id", adapters._compose_gemini_reasoning("gemini-3.7-flash", "high") == "gemini-3.7-flash-high")
ck("compose: an existing tier is REPLACED (explicit arg wins)", adapters._compose_gemini_reasoning("gemini-3.7-flash-medium", "high") == "gemini-3.7-flash-high")
ck("compose: a non-agy effort (minimal) has no suffix — id unchanged, tier never invented", adapters._compose_gemini_reasoning("gemini-3.7-flash", "minimal") == "gemini-3.7-flash")
ck("compose: no reasoning -> id unchanged", adapters._compose_gemini_reasoning("gemini-3.7-flash", None) == "gemini-3.7-flash")


print("\n-- (b) METERED path: an agy id reaches the SDK as the BARE model + reasoning_effort (was a 404 id) --")
_seen = {}


class _Msg:
    content = "ok"


class _Choice:
    message = _Msg()
    finish_reason = "stop"


class _Usage:
    prompt_tokens = 11
    completion_tokens = 3


class _Resp:
    choices = [_Choice()]
    usage = _Usage()


class _Completions:
    def create(self, **kw):
        _seen.clear()
        _seen.update(kw)                       # capture exactly what the metered API was asked to send
        return _Resp()


class _Chat:
    completions = _Completions()


class _OpenAI:
    def __init__(self, *a, **k):
        self.chat = _Chat()


_fake_openai = types.ModuleType("openai")
_fake_openai.OpenAI = _OpenAI
sys.modules["openai"] = _fake_openai

r = adapters._call_once("gemini-3.7-flash-medium", "hi", max_tokens=64, _skip_lane=True)
ck("no error — the agy suffix id no longer 404s on the metered API", not r.get("error"))
ck("the SDK received the BARE model id, not the agy suffix id", _seen.get("model") == "gemini-3.7-flash")
ck("the tier rode a reasoning_effort parameter (not lost)", str(_seen.get("reasoning_effort", "")).strip() != "")
ck("the result RECORDS the bare model actually served", r.get("model") == "gemini-3.7-flash" and r.get("executor") == "api")

# a non-suffixed id passes straight through, its reasoning param intact
r2 = adapters._call_once("gemini-flash-latest", "hi", max_tokens=64, reasoning="low", _skip_lane=True)
ck("a non-suffixed metered id is unchanged (no spurious split)", _seen.get("model") == "gemini-flash-latest")


print("\n-- (c) AGY LANE: a bare id + reasoning reaches run_prompt as the SUFFIXED id (tier not dropped) --")
_lane_seen = {}


def _fake_run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None):
    _lane_seen["model"] = model
    _lane_seen["reasoning"] = reasoning
    return {"text": "ok", "in_tok": 5, "out_tok": 1, "latency": 0.01, "error": None}


_fake_agy = types.SimpleNamespace(run_prompt=_fake_run_prompt, TIMEOUT_S=300)
adapters._lane_for = lambda prov: ("gemini", _fake_agy) if prov == "gemini" else None
adapters._lane_cooling = lambda lane: False

r3 = adapters._call_once("gemini-3.7-flash", "hi", max_tokens=64, reasoning="medium")
ck("the agy lane served it ($0), so run_prompt was reached", not r3.get("error") and r3.get("executor") == "gemini")
ck("run_prompt received the SUFFIXED agy id (bare id + reasoning=medium -> …-flash-medium)",
   _lane_seen.get("model") == "gemini-3.7-flash-medium")

# an id that is ALREADY an agy suffix id, with no reasoning arg, is handed to agy unchanged
r4 = adapters._call_once("gemini-3.7-flash-high", "hi", max_tokens=64)
ck("an already-suffixed agy id with no reasoning arg is passed through unchanged",
   _lane_seen.get("model") == "gemini-3.7-flash-high")

print(f"\n{'[FAIL]' if fails else 'OK'} test_gemini_reasoning_namespace: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
