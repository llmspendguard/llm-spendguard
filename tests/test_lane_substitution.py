"""Part 2 stage 2+3 — cross-plan substitution: route a HOT plan's work to an IDLE plan's model, honestly, and adapt
the prompt for the target. Guards, all offline (stubbed utilisation + a fake inner call; no LLM):
  • registry is propose→CONFIRM-once: only CONFIRMED substitutes route; pending never do;
  • route_decision is PROACTIVE (primary hot + idle confirmed substitute) and REACTIVE (primary failed), never onto a
    cooling lane, default OFF (no confirmed substitute → no change);
  • dispatch (_call_guarded) actually runs the substitute, RECORDS it as the model that answered + substituted_from;
  • Stage 3: a recorded adapted system is applied to the substitute (mechanically), flagged prompt_adapted.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanesub-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, adapters, calls                                   # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
INTENT = "patient-note-extract"

print("-- registry: propose records PENDING; only CONFIRM makes a substitute usable --")
lane_balance.record_proposal(INTENT, "anthropic:claude-opus-4-8", ["openai:gpt-5.5", "gemini:gemini-3.7-flash-high"],
                             proposed_by="claude-haiku-4-5")
fails += ck("proposed substitutes are PENDING", set(lane_balance.pending_for(INTENT)) ==
            {"openai:gpt-5.5", "gemini:gemini-3.7-flash-high"})
fails += ck("...and NOT yet usable (confirmed is empty)", lane_balance.substitutes_for(INTENT) == [])
lane_balance.confirm_substitute(INTENT, "openai:gpt-5.5")
fails += ck("confirm promotes ONE substitute to usable", lane_balance.substitutes_for(INTENT) == ["openai:gpt-5.5"])
fails += ck("...and removes it from pending", "openai:gpt-5.5" not in lane_balance.pending_for(INTENT))

print("\n-- route_decision: proactive shed only when primary HOT + substitute IDLE; default OFF otherwise --")
_util = lane_balance.lane_utilization
try:
    # claude-code HOT, codex IDLE (openai lane)
    lane_balance.lane_utilization = lambda: {"lanes": [
        {"lane": "claude-code", "state": "hot"}, {"lane": "codex", "state": "idle"},
        {"lane": "gemini", "state": "idle"}, {"lane": "zai-coding", "state": "idle"}]}
    sub, why = lane_balance.route_decision(INTENT, "anthropic:claude-opus-4-8")
    fails += ck("primary HOT + confirmed idle substitute → route to it", sub == "openai:gpt-5.5")
    # primary NOT hot → no shed
    lane_balance.lane_utilization = lambda: {"lanes": [{"lane": "claude-code", "state": "warm"},
                                                       {"lane": "codex", "state": "idle"}]}
    sub2, _ = lane_balance.route_decision(INTENT, "anthropic:claude-opus-4-8")
    fails += ck("primary NOT hot → no substitution (proactive)", sub2 is None)
    # reactive: primary failed → substitute regardless of primary heat
    sub3, _ = lane_balance.route_decision(INTENT, "anthropic:claude-opus-4-8", reactive=True)
    fails += ck("reactive (primary FAILED) → substitute even if primary not hot", sub3 == "openai:gpt-5.5")
    # never route onto a COOLING lane
    _cool = adapters._lane_cooling
    try:
        adapters._lane_cooling = lambda lane: lane == "codex"
        lane_balance.lane_utilization = lambda: {"lanes": [{"lane": "claude-code", "state": "hot"},
                                                           {"lane": "codex", "state": "idle"}]}
        sub4, _ = lane_balance.route_decision(INTENT, "anthropic:claude-opus-4-8")
        fails += ck("a COOLING substitute lane is skipped", sub4 is None)
    finally:
        adapters._lane_cooling = _cool
    # an intent with NO confirmed substitute → OFF
    fails += ck("intent with no confirmed substitute → None (default OFF)",
                lane_balance.route_decision("some-other-intent", "anthropic:claude-opus-4-8")[0] is None)
finally:
    lane_balance.lane_utilization = _util

print("\n-- dispatch: _call_guarded runs the substitute, records it honestly, applies the adapted prompt --")
seen = {}


def _fake_once(model, prompt, max_tokens=512, **kw):
    seen["model"] = model
    seen["system"] = kw.get("system")
    return {"provider": "x", "model": model, "text": "ok", "in_tok": 1, "out_tok": 1, "latency": 0.0,
            "cost": 0.0, "finish_reason": "stop", "error": None}


_o_once, _o_fits, _o_route, _o_adapt = adapters._call_once, adapters._input_fits, lane_balance.route_decision, lane_balance.adapted_system_for
try:
    adapters._call_once = _fake_once
    adapters._input_fits = lambda *a, **k: (True, "stubbed")
    lane_balance.route_decision = lambda intent, model, reactive=False: (
        ("openai:gpt-5.5", "primary hot") if model == "anthropic:claude-x" else (None, ""))
    lane_balance.adapted_system_for = lambda intent, target: None
    with calls.context(intent=INTENT):
        r = adapters._call_guarded("anthropic:claude-x", "hi", max_tokens=100, system="SYS")
    fails += ck("proactive substitution ran the SUBSTITUTE model", seen.get("model") == "openai:gpt-5.5")
    fails += ck("...recorded as the substitute (model that answered)", r.get("model") == "openai:gpt-5.5")
    fails += ck("...with substituted_from = the primary (honest provenance)", r.get("substituted_from") == "anthropic:claude-x")

    seen.clear()
    with calls.context(intent=INTENT):
        r2 = adapters._call_guarded("zzz:no-sub-model", "hi", max_tokens=100)
    fails += ck("a model with NO confirmed substitute is UNCHANGED (default off)",
                seen.get("model") == "zzz:no-sub-model" and "substituted_from" not in r2)

    seen.clear()
    lane_balance.adapted_system_for = lambda intent, target: "ADAPTED::" + target
    with calls.context(intent=INTENT):
        r3 = adapters._call_guarded("anthropic:claude-x", "hi", max_tokens=100, system="SYS")
    fails += ck("Stage 3: the substitute receives the RECORDED adapted system (mechanical)",
                seen.get("system") == "ADAPTED::openai:gpt-5.5")
    fails += ck("...and the result flags prompt_adapted", r3.get("prompt_adapted") is True)
finally:
    adapters._call_once, adapters._input_fits = _o_once, _o_fits
    lane_balance.route_decision, lane_balance.adapted_system_for = _o_route, _o_adapt

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_substitution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
