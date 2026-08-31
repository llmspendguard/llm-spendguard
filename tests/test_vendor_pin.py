"""Vendor pinning + answered-path provenance — the fix for the lane-bandit cross-vendor-panel collapse.

The lane bandit (advisor.lane_bandit + bandit_mode=optout) can run ONE model 'in place of' four while every result
keeps its REQUESTED label — so a 'cross-vendor consensus' silently becomes one model agreeing with itself. This
guards the two halves of the resolution:
  1. call(..., no_substitution=True) PINS the vendor — suppresses BOTH the proactive bandit swap AND the reactive
     failover swap, so the requested model answers or errors, never a silent substitution.
  2. the RESULT already carries who actually answered — was_substituted()/served_by()/panel_providers() read it, so
     a panel asserts diversity FROM THE RESULTS (never assumes it) without grepping the run log for 'in place of'.

Offline: _call_once and route_decision are monkeypatched; no network, no lanes, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-pin-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters
from spendguard import lane_balance

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# a deterministic leaf: echo the model actually asked for + record the _no_sub flag that reached it (no network)
_seen = {}
def fake_once(model, prompt, max_tokens=None, _no_sub=False, **kw):
    _seen["_no_sub"] = _no_sub
    return {"provider": adapters.provider_for(model), "model": model, "text": "ok", "in_tok": 1, "out_tok": 1,
            "cost": 0.0, "executor": "api", "finish_reason": "stop", "error": None}
adapters._call_once = fake_once

REQ = "anthropic:claude-opus-4-8"

print("-- (1) proactive bandit swap: no_substitution=False substitutes, =True PINS --")
lane_balance.route_decision = lambda intent, model, reactive=False: ("gemini:gemini-3-flash", "bandit → gemini")
r_sub = adapters.call(REQ, "p", max_tokens=10)                       # bandit eligible → swapped
ck("default: the bandit swap happens (substituted_from set, a DIFFERENT model answered)",
   adapters.was_substituted(r_sub) and r_sub["substituted_from"] == REQ and r_sub["model"] == "gemini:gemini-3-flash")
ck("served_by reads the ACTUAL answering vendor (gemini), not the requested (anthropic)",
   adapters.served_by(r_sub) == "gemini")
r_pin = adapters.call(REQ, "p", max_tokens=10, no_substitution=True)  # vendor pinned
ck("no_substitution=True → NO swap (the requested model answered)",
   not adapters.was_substituted(r_pin) and r_pin["model"] == REQ and adapters.served_by(r_pin) == "anthropic")

print("-- (2) panel COLLAPSE is visible from the results (no log-grep needed) --")
FOUR = [REQ, "openai:gpt-5.5", "zai:glm-4.6", "moonshot:kimi-k3"]
collapsed = [adapters.call(m, "p", max_tokens=10) for m in FOUR]      # every one swapped to gemini
ck("a supposed 4-vendor panel COLLAPSED to 1 actual vendor — panel_providers exposes it",
   len(adapters.panel_providers(collapsed)) == 1 and adapters.panel_providers(collapsed) == {"gemini"})
ck("every collapsed result still carries its REQUESTED label (why keying on the request hides the collapse)",
   [c["substituted_from"] for c in collapsed] == FOUR)
pinned = [adapters.call(m, "p", max_tokens=10, no_substitution=True) for m in FOUR]
ck("pinned panel keeps all 4 vendors distinct", len(adapters.panel_providers(pinned)) == 4)

print("-- (3) the pin reaches the REACTIVE seam too (flag threaded call → _call_guarded → _call_once) --")
lane_balance.route_decision = lambda intent, model, reactive=False: (None, "")   # no proactive swap now
adapters.call("openai:gpt-5.5", "p", max_tokens=10, no_substitution=True)
ck("_call_once receives _no_sub=True (so its reactive-failover seam is pinned)", _seen["_no_sub"] is True)
adapters.call("openai:gpt-5.5", "p", max_tokens=10)
ck("_call_once receives _no_sub=False by default (substitution stays available for ordinary work)",
   _seen["_no_sub"] is False)

print("-- (4) helpers are honest on edge cases --")
ck("served_by falls back to executor when there's no model", adapters.served_by({"executor": "codex"}) == "codex")
ck("an errored result is not counted as an answering vendor",
   adapters.panel_providers([{"error": "boom", "model": "x:y"}, {"model": "openai:gpt-5.5", "error": None}]) == {"openai"})

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"vendor_pin: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
