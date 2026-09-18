"""The $0 lane path must RECEIVE the guarded output budget, so _call_guarded's escalation ladder can RAISE a lane's
cap on a truncation instead of the lane being pinned to its own budget forever.

The gap: _call_once handed the lane run_prompt no max_tokens — the lane sized its own cap, disconnected from the
ladder, so a truncated lane reply could not be retried larger the way a metered one is. This pins the fix: (1) the
lane run_prompt protocol accepts max_tokens uniformly, and (2) _call_once forwards the guarded budget to it.

Hermetic: adapters._lane_for is stubbed to a fake lane that records the max_tokens it was handed — no network, no CLI."""
import inspect
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lanebudget-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, zai_exec, codex_exec, subscription_exec, antigravity_exec   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


print("-- every lane's run_prompt accepts max_tokens (the uniform protocol the ladder relies on) --")
for mod in (zai_exec, codex_exec, subscription_exec, antigravity_exec):
    params = inspect.signature(mod.run_prompt).parameters
    ck(f"{mod.__name__.split('.')[-1]}.run_prompt accepts max_tokens", "max_tokens" in params)

print("\n-- _call_once forwards the guarded budget to the lane's run_prompt --")
_seen = {}


class _FakeLane:
    TIMEOUT_S = 300
    MIN_TIMEOUT_S = 30

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None):
        _seen["max_tokens"] = max_tokens
        return {"text": "ok", "in_tok": 3, "out_tok": 2, "latency": 0.1, "error": None}


adapters._lane_for = lambda prov: ("fakelane", _FakeLane)
r = adapters._call_once("openai:gpt-5-nano", "hi", max_tokens=12345)
ck("the lane served the call (not a metered fallback)", r.get("executor") == "fakelane")
ck("the lane run_prompt received the exact guarded budget (12345), enabling escalation",
   _seen.get("max_tokens") == 12345)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_receives_budget: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
