"""Two DIFFERENT knobs that used to be conflated: 'don't swap to a different MODEL' vs 'don't fall back to a
different EXECUTOR (lane→metered) for the SAME model'. Conflating them pinned a dead LANE and refused the working
metered API for the same model (kimi-k3 / gemini answer fine metered) — the reliability hangs.

Locked here so the separation cannot silently regress:
  • no_substitution=True (pin the vendor/model) STILL falls back to the metered API for the SAME model when its
    lane misses — the requested model is unchanged, executor='api', and the metered call actually happened;
  • no_metered_fallback=True suppresses that fallback ONLY for a TASK miss — a lane that RAN but returned no usable
    text (empty / off-shape) yields a refusal row ($0, attributed to the LANE) and the metered API is NEVER called.
    But a lane that is DOWN (its executor returned an error — auth/token expired, CLI crash, rejected model) is an
    INFRASTRUCTURE failure, not an unsuitable task, so it STILL fails over to the metered API even under
    no_metered_fallback (Ash 2026-09-26: "a down lane still fails over" — the empty-and-skipped a logged-out lane
    produced is the bug this closes; budget_usd is the hard $0 cap for a caller that truly must never bill);
  • no_substitution gates ONLY the DIFFERENT-model reactive failover: route_decision is consulted when it is
    False and skipped when it is True.
The two no_metered_fallback outcomes are told apart STRUCTURALLY (which branch set _lane_reason: 'empty' task miss
vs 'lane_error' executor error), never by a substring of the error prose. Offline: the lane, the OpenAI SDK client,
the key, served-check and route_decision are stubbed — no network, no spend.
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
    """A lane that is DOWN — its executor returns an ERROR (auth/crash/rejected). Infra failure: the call must fail
    over past it to the metered API even under no_metered_fallback."""
    MIN_TIMEOUT_S = 1
    TIMEOUT_S = 5

    def run_prompt(self, prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None, **_kw):
        return {"error": "lane down (stubbed)"}


class _EmptyLane:
    """A lane that RAN but returned no usable text (a TASK miss, not an infra failure) — the case a $0-only caller
    still refuses ($0), because paying metered to retry a task the free lane found unsuitable is what --refuse-billed
    opts out of."""
    MIN_TIMEOUT_S = 1
    TIMEOUT_S = 5

    def run_prompt(self, prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None, **_kw):
        return {"text": "", "error": None}


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

# ── no_metered_fallback suppresses the fallback ONLY for a TASK miss; a DOWN lane still fails over ──
# (a) a DOWN lane (executor error) is INFRA failure → it STILL fails over to the metered API even under refuse_billed
#     (Ash 2026-09-26: "a down lane still fails over" — never lose work to a logged-out lane; budget_usd is the hard $0)
adapters._lane_for = lambda prov: ("deadlane", _DeadLane())
r = _run(no_substitution=True, no_metered_fallback=True)
ck("refuse_billed + a DOWN lane (error) STILL fails over to the metered API (infra, not a task miss)",
   _metered_calls == ["deepseek-v4-flash"] and r.get("executor") == "api" and r.get("error") is None)
# (b) a TASK miss (ran, empty, NO error) under refuse_billed → $0 refusal, metered NEVER called (the preserved contract).
#     Asserted structurally: the metered leg never ran, the row is attributed to the LANE, nothing was answered/charged.
adapters._lane_for = lambda prov: ("emptylane", _EmptyLane())
r = _run(no_substitution=True, no_metered_fallback=True)
ck("refuse_billed + a TASK miss (empty) → the metered API is NEVER called ($0 by construction)", _metered_calls == [])
ck("...and the miss is attributed to the LANE executor, not 'api'", r.get("executor") == "emptylane")
ck("...and nothing was answered or charged (a refusal row, not an answer)", r.get("text") is None and r.get("cost") is None and r.get("error") is not None)

# ── no_substitution=False DOES consult the different-model failover (the other half of the separation) ──
adapters._lane_for = lambda prov: ("deadlane", _DeadLane())    # a failing lane → the reactive different-model path
r = _run(no_substitution=False, no_metered_fallback=False)
ck("unpinned (no_substitution=False): route_decision IS consulted for a different-model failover", len(_route_calls) == 1 and _route_calls[0][2] is True)
ck("...and with no confirmed substitute it STILL reaches the same-model metered API", _metered_calls == ["deepseek-v4-flash"] and r.get("executor") == "api")

print(("[OK]" if not fails else "[FAIL]") + " lane/metered fallback separation: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
