"""GUARD: cold best-value pins keep healthy lanes and recover past known-red families.

Public adapters.call and its real recovery ladder run against fake transports and cached facts; no network.
"""
import os
import sys
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-floor-health-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_DEFAULT_REASONING"] = "best-value"
os.environ["SPENDGUARD_AVOID_CLAUDE_LANE"] = "off"
os.environ["SPENDGUARD_LANE_BACKOFF_S"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import openai  # noqa: E402
from spendguard import (adapters, advisor, best_value, config, gate, lane_balance, lane_catalog,  # noqa: E402
                        lane_queue, lane_quota_cost, lanes, plan_admission, pricing, reasoning_equivalence,
                        reliability, route_utility, vendor_call)

FLOOR_MODELS = {"openai": "openai:gpt-5.6-sol", "anthropic": "anthropic:claude-sonnet-4-6",
                "gemini": "gemini:gemini-3-flash-preview"}
FLOOR_REPLY = '{"reviewed":true}'
FLOOR_BUDGET = 128
FLOOR_HEADROOM = 75
floor_health_state = {"down": False, "free": True, "metered_red": False, "quota_zero": True, "family_red": True}
floor_health_invocations = []
floor_health_pins = []


def floor_health_lane_reply(provider, prompt, **kwargs):
    floor_health_invocations.append((provider, "lane"))
    if provider == "anthropic" or (provider == "openai" and floor_health_state["down"]):
        return {"error": "transport unavailable", "transport_error": True}
    return {"text": FLOOR_REPLY, "error": None, "in_tok": 1, "out_tok": 1}


def floor_health_select_pin(intent, requested_model, pin_model=False, **kwargs):
    floor_health_pins.append(pin_model)
    return {"model": None, "effort": None, "why": "no learned effort; family floor stands"}


def floor_health_red_resources():
    providers = (["anthropic"] if floor_health_state["family_red"] else [])
    providers += ["openai"] if floor_health_state["metered_red"] else []
    return [{"resource": provider, "kind": "metered", "reason": "unreachable"} for provider in providers]


def floor_health_headroom(do_fetch=False):
    return [{"lane": adapters._LANES[provider][0], "provider": provider, "known": True, "reset_ts": None,
             "remaining_pct": 0 if provider == "anthropic" and floor_health_state["quota_zero"] else FLOOR_HEADROOM}
            for provider in FLOOR_MODELS]


def floor_health_metered_create(**kwargs):
    floor_health_invocations.append(("openai", "metered"))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=FLOOR_REPLY), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


FLOOR_TRANSPORTS = {provider: SimpleNamespace(
    TIMEOUT_S=FLOOR_BUDGET, MIN_TIMEOUT_S=1,
    run_prompt=lambda prompt, _provider=provider, **kwargs: floor_health_lane_reply(_provider, prompt, **kwargs))
    for provider in FLOOR_MODELS}


def floor_health_dispatch_case(reasoning=None):
    floor_health_invocations.clear()
    floor_health_pins.clear()
    return adapters.call(FLOOR_MODELS["openai"], "review whole change", intent="honestreview:floor-health-guard",
                         max_tokens=FLOOR_BUDGET, reasoning=reasoning, _no_guard=True, base_fallback=False)


def run_pinned_floor_health_guard():
    measured_free_substitutes = lane_balance.quality_equivalent_free_substitutes
    with ExitStack() as stack:
        replacements = [
            (adapters, "_lane_for", lambda provider: (adapters._LANES[provider][0], FLOOR_TRANSPORTS[provider])),
            (adapters, "_lane_too_big", lambda *args: False),
            (adapters, "_lane_model_cooling", lambda *args: False),
            (adapters, "_lane_cooling", lambda *args: False),
            (adapters, "_manage_all_enabled", lambda: False),
            (adapters, "_book_substitution", lambda *args: None),
            (best_value, "select_model_effort", floor_health_select_pin),
            (vendor_call, "served_substitute", lambda provider, model: (model, None)),
            (vendor_call, "served_check", lambda *args: "served"),
            (lanes, "lane_auth_status", lambda *args: {"authed": True}),
            (lanes, "lane_headroom", floor_health_headroom),
            (lanes, "lanes_status", lambda: {"lanes": [
                {"lane": adapters._LANES[provider][0], "enabled": provider != "gemini" or floor_health_state["free"],
                 "auth": "ok"} for provider in FLOOR_MODELS]}),
            (lane_catalog, "lanes", lambda: [adapters._LANES[provider][0] for provider in FLOOR_MODELS]),
            (lane_catalog, "lane_model_for_tier", lambda lane, tier: next(
                model for provider, model in FLOOR_MODELS.items() if adapters._LANES[provider][0] == lane)),
            (lane_balance, "quality_equivalent_free_substitutes", lambda *args, **kwargs: [FLOOR_MODELS["anthropic"]]),
            (route_utility, "rank_lanes", lambda rows, **kwargs: [dict(row, available=True) for row in rows]),
            (reliability, "health_reds", floor_health_red_resources),
            (lane_queue, "hold_free_recovery", lambda *args, **kwargs: {
                "error": "isolated queue deadline", "_free_hold": {"deadline_exhausted": True}}),
            (lane_quota_cost, "apply_anthropic_avoidance", lambda *args, **kwargs: None),
            (lane_quota_cost, "measured_lane_token_cost", lambda *args: {}),
            (reasoning_equivalence, "resolve_metered", lambda *args: {
                "metered_model": FLOOR_MODELS["openai"].split(":", 1)[1], "metered_effort": "minimal"}),
            (config, "api_key", lambda *args: "isolated-transport"),
            (config, "shed_lanes", lambda: set()),
            (pricing, "realtime_cost", lambda *args, **kwargs: 0.0),
            (gate, "_rt_precheck", lambda *args, **kwargs: None),
            (openai, "OpenAI", lambda **kwargs: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=floor_health_metered_create)))),
        ]
        for owner, symbol, replacement in replacements:
            stack.enter_context(patch.object(owner, symbol, replacement))

        healthy = floor_health_dispatch_case()
        assert healthy.get("text") == FLOOR_REPLY and healthy.get("executor") == adapters._LANES["openai"][0]
        assert floor_health_invocations == [("openai", "lane")] and floor_health_pins == [True]

        # Exercise the actual quality helper, whose candidates must share admission readiness.
        confirmed_models = [FLOOR_MODELS["anthropic"], FLOOR_MODELS["gemini"]]
        with patch.object(lane_balance, "substitutes_for", return_value=confirmed_models), patch.object(
                advisor, "recommend_models", return_value={
                    "top": [{"id": candidate, "meets_bar": True} for candidate in confirmed_models]}):
            for zero_quota, family_red in ((True, True), (False, True), (True, False)):
                floor_health_state.update(quota_zero=zero_quota, family_red=family_red)
                assert measured_free_substitutes("honestreview:floor-health-guard", FLOOR_MODELS["openai"]) == [
                    FLOOR_MODELS["gemini"]]
            with patch.object(adapters, "_lane_cooling", side_effect=lambda lane: lane == adapters._LANES["gemini"][0]):
                assert plan_admission._ready(adapters._LANES["openai"][0])
                assert not plan_admission._ready(adapters._LANES["gemini"][0])
                assert measured_free_substitutes("honestreview:floor-health-guard", FLOOR_MODELS["openai"]) == []
        floor_health_state.update(quota_zero=True, family_red=True)

        minimal_healthy = floor_health_dispatch_case(reasoning="minimal")
        assert minimal_healthy.get("text") == FLOOR_REPLY
        assert minimal_healthy.get("executor") == adapters._LANES["openai"][0]
        assert floor_health_invocations == [("openai", "lane")]
        floor_health_state["down"] = True
        minimal_recovered = floor_health_dispatch_case(reasoning="minimal")
        assert minimal_recovered.get("text") == FLOOR_REPLY and not minimal_recovered.get("error")
        assert minimal_recovered.get("executor") == adapters._LANES["gemini"][0], minimal_recovered
        assert all(provider != "anthropic" for provider, _ in floor_health_invocations)
        floor_health_state["down"] = False

        floor_health_state["down"] = True
        for zero_quota, family_red in ((True, True), (False, True), (True, False)):
            floor_health_state["quota_zero"] = zero_quota
            floor_health_state["family_red"] = family_red
            recovered = floor_health_dispatch_case()
            assert recovered.get("text") == FLOOR_REPLY and not recovered.get("error"), recovered
            assert recovered.get("executor") == adapters._LANES["gemini"][0], recovered
            assert all(provider != "anthropic" for provider, _ in floor_health_invocations), floor_health_invocations
            assert ("openai", "metered") not in floor_health_invocations
            assert floor_health_pins[0] is True

        floor_health_state["free"] = False
        metered = floor_health_dispatch_case()
        assert metered.get("text") == FLOOR_REPLY and not metered.get("error"), metered
        assert ("openai", "metered") in floor_health_invocations
        assert all(provider != "anthropic" for provider, _ in floor_health_invocations)

        floor_health_state["metered_red"] = True
        try:
            floor_health_dispatch_case()
        except gate.SpendGateRefused:
            assert ("openai", "metered") not in floor_health_invocations
        else:
            raise AssertionError("all routes unreachable must raise a typed deliberate stop")

        # Exercise the public consumer boundary independently of the transport ladder above.
        floor_health_state["down"] = False
        floor_health_state["metered_red"] = False
        for unusable_reply in ({"text": "", "error": None}, {"text": None, "error": "isolated transport failure"}):
            with patch.object(adapters, "_call_guarded", return_value={
                    **unusable_reply, "lane_recovery": [{"rung": "metered_outcome", "outcome": "failed"}],
                    "provider": "openai", "model": FLOOR_MODELS["openai"].split(":", 1)[1],
                    "cost": None, "executor": "api"}):
                try:
                    adapters.call(FLOOR_MODELS["openai"], "review whole change", reasoning="minimal",
                                  intent="honestreview:floor-health-guard", base_fallback=False)
                except gate.SpendGateRefused:
                    pass
                else:
                    raise AssertionError("labelled recovery must never return an empty or error review")
    print("OK test_pinned_floor_health_recovery: healthy pin, red/quota exclusion, metered twin, typed stop")


if __name__ == "__main__":
    run_pinned_floor_health_guard()
