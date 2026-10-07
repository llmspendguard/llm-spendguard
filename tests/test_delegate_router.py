"""Offline contract for the classify → route → split-axis receipt path.

The lane classifier and Codex executor are stubbed; no provider CLI, network, or paid API can run.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-delegate-router-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec, config, delegate_router, gate, guard, lane_balance, lanes, pricing, reliability  # noqa: E402


_failed = []


def check(label, condition):
    ok = bool(condition)
    if not ok:
        _failed.append(label)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
original_config = config._cfg_get
original_lane = lane_balance.delegate
original_codex = codex_exec.run_prompt
original_codex_available = codex_exec.available
original_codex_auth_status = codex_exec.auth_status
original_price = pricing.realtime_cost
original_ratio = delegate_router._measured_claude_overage_ratio
original_decision = guard.record_decision
original_saving = guard.record_saving
original_require = gate.require
original_lanes_status = lanes.lanes_status
original_health_reds = reliability.health_reds
lane_calls = []
codex_calls = []
decision_calls = []
saving_calls = []
classification = {"kind": "oneshot", "provider": "codex", "self_contained": True, "why": "independent answer"}
configured_models = {"codex": {"cheap": "test-cheap", "strong": "test-strong"}}


def fake_config(section, key, default=None):
    if (section, key) == ("advisor", "lane_models"):
        return configured_models
    if (section, key) == ("advisor", "route_est_out"):
        return 40
    if (section, key) == ("caps", "intent_caps"):
        return {}
    return original_config(section, key, default)


def fake_lane(prompt, **kwargs):
    lane_calls.append((prompt, kwargs))
    if kwargs.get("intent") == delegate_router.CLASSIFY_INTENT:
        return {"text": json.dumps(classification), "lane": "codex", "model": "openai:test-cheap"}
    return {"text": "one-shot result", "lane": "codex", "model": "openai:test-cheap", "cost": 0.0,
            "in_tok": 20, "out_tok": 10}


def fake_codex(prompt, **kwargs):
    codex_calls.append((prompt, kwargs))
    return {"text": "agentic result", "in_tok": 80, "out_tok": 20, "error": None}


try:
    config._cfg_get = fake_config
    lane_balance.delegate = fake_lane
    codex_exec.run_prompt = fake_codex
    codex_exec.available = lambda: True
    codex_exec.auth_status = lambda: {"authed": True}
    pricing.realtime_cost = lambda model, in_tok, out_tok, provider=None: (in_tok + out_tok) / 1000
    delegate_router._measured_claude_overage_ratio = lambda: 0.5
    guard.record_decision = lambda **kwargs: decision_calls.append(kwargs)
    guard.record_saving = lambda *args, **kwargs: saving_calls.append((args, kwargs))
    gate.require = lambda: None
    lanes.lanes_status = lambda: {"executor": "pool", "lanes": [
        {"lane": "codex", "enabled": True, "cli": "/fake/codex", "auth": "ok", "activate": None}]}
    reliability.health_reds = lambda: []

    print("-- delegation readiness is split by route kind --")
    readiness = delegate_router.delegation_lanes_ready("oneshot")
    check("configured installed reachable lane is ready", readiness["ready"] == ["codex"])
    check("doctor line renders both route kinds",
          "oneshot codex (1)" in delegate_router.delegation_doctor_line()
          and "agentic codex (1)" in delegate_router.delegation_doctor_line())
    reliability.health_reds = lambda: [{"resource": "codex", "kind": "lane"}]
    check("cached health-down state removes an otherwise installed lane",
          delegate_router.delegation_lanes_ready("oneshot")["ready"] == [])
    check("agentic readiness ignores one-shot lane health and uses the executor's auth check",
          delegate_router.delegation_lanes_ready("agentic")["ready"] == ["codex"])
    reliability.health_reds = lambda: []

    print("-- validates every classifier kind and configured provider --")
    for kind in ("oneshot", "agentic", "needs_claude"):
        classification.update(kind=kind, self_contained=(kind != "needs_claude"),
                              provider=(None if kind == "needs_claude" else "codex"))
        judged = delegate_router.classify_task("do it")
        check(f"validated {kind}", judged.get("kind") == kind and not judged.get("error"))

    classification.update(kind="bogus", provider="codex", self_contained=True)
    check("invalid kind is refused", "error" in delegate_router.classify_task("do it"))
    configured_models["gemini"] = {"cheap": "gemini-cheap", "strong": "gemini-strong"}
    classification.update(kind="oneshot", provider="gemini", self_contained=True, why="off-host answer")
    excluded = delegate_router.classify_task("exclusion-check", exclude_plans=["codex"])
    classifier_prompt = lane_calls[-1][0]
    check("classifier exclusions remove the hosting lane from the supplied plan menu",
          excluded.get("provider") == "gemini" and 'CONFIGURED PLAN NAMES: ["gemini"]' in classifier_prompt)
    configured_models.pop("gemini")

    print("-- dry-run classifies and estimates but executes nothing --")
    classification.update(kind="agentic", provider="codex", self_contained=True, why="self-contained repo task")
    before_codex = len(codex_calls)
    dry = delegate_router.delegate_task("edit it", execute=False)
    check("dry-run returns estimate", dry["status"] == "estimate")
    check("dry-run never invokes codex executor", len(codex_calls) == before_codex)
    check("agentic route selects configured strong model", dry["estimate"]["model"] == "test-strong")

    print("-- a down one-shot lane cannot falsely refuse an authenticated agentic Codex route --")
    lanes.lanes_status = lambda: {"executor": "pool", "lanes": [
        {"lane": "codex", "enabled": True, "cli": None, "auth": "missing", "activate": "install/login"}]}
    classification.update(kind="agentic", provider="codex", self_contained=True, why="self-contained repo task")
    authenticated_agent = delegate_router.delegate_task("edit it", provider="codex")
    check("agentic Codex remains ready when its CLI is present+authed despite the one-shot probe being down",
          authenticated_agent["status"] == "estimate")
    classification.update(kind="oneshot", provider="codex", self_contained=True, why="independent answer")
    unavailable_oneshot = delegate_router.delegate_task("answer it", provider="codex")
    check("oneshot Codex still refuses when the one-shot lane probe is down",
          unavailable_oneshot["status"] == "refused" and unavailable_oneshot["ready_alternatives"] == [])
    classification.update(kind="agentic", provider="codex", self_contained=True, why="self-contained repo task")
    codex_exec.auth_status = lambda: {"authed": False}
    unavailable_agent = delegate_router.delegate_task("edit it", provider="codex")
    check("genuinely unauthenticated agent CLI retains the typed refusal",
          unavailable_agent["status"] == "refused" and unavailable_agent["ready_alternatives"] == [])
    codex_exec.auth_status = lambda: {"authed": True}
    lanes.lanes_status = lambda: {"executor": "pool", "lanes": [
        {"lane": "codex", "enabled": True, "cli": "/fake/codex", "auth": "ok", "activate": None}]}
    configured_models["gemini"] = {"cheap": "gemini-cheap", "strong": "gemini-strong"}
    classification.update(kind="agentic", provider="gemini", self_contained=True, why="bounded")
    selected_unavailable = delegate_router.delegate_task("edit it")
    check("classifier cannot route to an unavailable configured plan and names ready alternatives",
          selected_unavailable["status"] in ("error", "refused") and "codex" in str(selected_unavailable))
    configured_models.pop("gemini")

    print("-- needs_claude is a typed refusal with no hop-2 execution --")
    classification.update(kind="needs_claude", provider=None, self_contained=False, why="needs live session")
    before_lane, before_codex = len(lane_calls), len(codex_calls)
    refused = delegate_router.delegate_task("continue that", execute=True)
    check("typed refusal returned", refused["status"] == "refused")
    check("no hop-2 ran", len(lane_calls) == before_lane + 1 and len(codex_calls) == before_codex)

    print("-- oneshot dispatches to lane; agentic dispatches to writable Codex --")
    classification.update(kind="oneshot", provider="codex", self_contained=True, why="one independent answer")
    one = delegate_router.delegate_task("answer", execute=True)
    check("oneshot ran through lane", one["execution"]["text"] == "one-shot result")
    classification.update(kind="agentic", provider="codex", self_contained=True, why="self-contained repo task")
    agent = delegate_router.delegate_task("edit and test", execute=True, cwd=os.path.dirname(__file__))
    check("agentic ran through codex", agent["execution"]["text"] == "agentic result")
    check("codex received workspace-write sandbox", codex_calls[-1][1].get("sandbox") == "workspace-write")
    check("codex received the explicit trusted workspace", codex_calls[-1][1].get("cwd") == os.path.dirname(__file__))
    check("routing decision was recorded with delegate basis",
          decision_calls[-1].get("basis") == "delegate" and decision_calls[-1].get("why") == "self-contained repo task")
    check("avoided overage was booked in the existing savings store",
          saving_calls[-1][0][0] == "delegate" and saving_calls[-1][0][1] == 0.05)
    receipt = agent["receipt"]
    check("receipt exposes separate real-$, est-value, and saved axes",
          set(("real_api_usd", "est_value_usd", "saved_overage_usd")) <= set(receipt)
          and receipt["real_api_usd"] == 0 and receipt["est_value_usd"] == 0.1
          and receipt["saved_overage_usd"] == 0.05)
finally:
    config._cfg_get = original_config
    lane_balance.delegate = original_lane
    codex_exec.run_prompt = original_codex
    codex_exec.available = original_codex_available
    codex_exec.auth_status = original_codex_auth_status
    pricing.realtime_cost = original_price
    delegate_router._measured_claude_overage_ratio = original_ratio
    guard.record_decision = original_decision
    guard.record_saving = original_saving
    gate.require = original_require
    lanes.lanes_status = original_lanes_status
    reliability.health_reds = original_health_reds

print(f"\n{'[FAIL]' if _failed else 'OK'} test_delegate_router: {len(_failed)} failure(s)")
sys.exit(1 if _failed else 0)
