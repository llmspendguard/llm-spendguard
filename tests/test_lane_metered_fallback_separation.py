"""Two DIFFERENT knobs that used to be conflated: 'don't swap to a different MODEL' vs 'don't fall back to a
different EXECUTOR (lane→metered) for the SAME model'. Conflating them pinned a dead LANE and refused the working
metered API for the same model (kimi-k3 / gemini answer fine metered) — the reliability hangs.

Locked here so the separation cannot silently regress:
  • no_substitution=True (pin the vendor/model) STILL falls back to the metered API for the SAME model when its
    lane misses — the requested model is unchanged, executor='api', and the metered call actually happened;
  • no_metered_fallback=True is the ONLY knob that suppresses that fallback — a lane miss returns a refusal row
    ($0, attributed to the LANE) and the metered API is NEVER called;
  • no_substitution gates ONLY the DIFFERENT-model reactive failover: route_decision is consulted when it is
    False and skipped when it is True.
Refusal is asserted on STRUCTURED signals (executor is the lane, the metered leg never ran, cost/text are null),
never on a substring of the error prose. Offline: the lane, the OpenAI SDK client, the key, served-check and
route_decision are stubbed — no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fallback-sep-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import openai                                                    # noqa: E402
from spendguard import adapters, vendor_call, config, pricing, lane_balance   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


MODEL = "deepseek:deepseek-v4-flash"            # a kind='openai' metered provider WITH a (stubbed) lane in front


class _DeadLane:
    """A lane that MISSES every prompt — the down/flaky lane (agy-style) the call must degrade past."""
    MIN_TIMEOUT_S = 1
    TIMEOUT_S = 5

    def run_prompt(self, prompt, system=None, model=None, timeout=None, reasoning=None):
        return {"error": "lane down (stubbed)"}


class _Msg:
    content = "metered-answer-same-model"


class _Choice:
    message = _Msg()
    finish_reason = "stop"


class _Usage:
    prompt_tokens, completion_tokens = 10, 5


class _FakeCompletion:
    choices = [_Choice()]
    usage = _Usage()


_metered_calls = []


class _FakeOpenAI:
    """Stands in for the OpenAI SDK client on the METERED leg; records that the metered API was actually hit."""
    def __init__(self, **kw):
        self.chat = self
        self.completions = self

    def create(self, **kw):
        _metered_calls.append(kw.get("model"))
        return _FakeCompletion()


# ── stubs: force a lane in front of the metered provider, and make the metered leg observable & offline ──
adapters._lane_for = lambda prov: ("deadlane", _DeadLane())
adapters._lane_too_big = lambda *a, **k: False
adapters._lane_model_cooling = lambda *a, **k: False
config.api_key = lambda env: "k"
vendor_call.served_check = lambda v, m: "served"                 # skip the stale-id pre-flight on the metered leg
vendor_call.served_substitute = lambda v, m: (m, None)          # no agentic id resolution in this test
pricing.realtime_cost = lambda m, i, o, **k: 0.001
adapters._book_substitution = lambda *a, **k: None
openai.OpenAI = _FakeOpenAI

_route_calls = []


def _spy_route(intent, model, reactive=False):
    _route_calls.append((intent, model, reactive))
    return (None, "")                                            # no confirmed substitute → falls through to metered


lane_balance.route_decision = _spy_route


def _run(**kw):
    """One raw dispatch through call(_no_guard=True) → _call_once, with the lane in front (it will miss)."""
    _metered_calls.clear()
    _route_calls.clear()
    return adapters.call(MODEL, "hello", max_tokens=32, _no_guard=True, **kw)


# ── no_substitution=True STILL falls back to the SAME-model metered API when the lane misses ──
r = _run(no_substitution=True, no_metered_fallback=False)
ck("pinned (no_substitution=True): a lane miss falls back to the metered API", r.get("executor") == "api" and r.get("error") is None)
ck("...and the metered call actually happened for the SAME model", _metered_calls == ["deepseek-v4-flash"])
ck("...and the model was NOT swapped (no different-model substitution)", r.get("model") == "deepseek-v4-flash" and "substituted_from" not in r)
ck("...and no_substitution=True SKIPS the different-model failover (route_decision not consulted)", _route_calls == [])

# ── no_metered_fallback=True is the ONLY knob that suppresses the same-model fallback ──
# Asserted structurally: the metered leg never ran, the row is attributed to the LANE, and nothing was answered/charged.
r = _run(no_substitution=True, no_metered_fallback=True)
ck("refuse_billed: the metered API is NEVER called ($0 by construction)", _metered_calls == [])
ck("...and the miss is attributed to the LANE executor, not 'api'", r.get("executor") == "deadlane")
ck("...and nothing was answered or charged (a refusal row, not an answer)", r.get("text") is None and r.get("cost") is None and r.get("error") is not None)

# ── no_substitution=False DOES consult the different-model failover (the other half of the separation) ──
r = _run(no_substitution=False, no_metered_fallback=False)
ck("unpinned (no_substitution=False): route_decision IS consulted for a different-model failover", len(_route_calls) == 1 and _route_calls[0][2] is True)
ck("...and with no confirmed substitute it STILL reaches the same-model metered API", _metered_calls == ["deepseek-v4-flash"] and r.get("executor") == "api")

print(("[OK]" if not fails else "[FAIL]") + " lane/metered fallback separation: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
