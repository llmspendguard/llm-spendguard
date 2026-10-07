"""Offline guards for plan-axis admission, redirect provenance, and caller propagation."""
import os
import pathlib
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-plan-admission-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_CALLS"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import adapters, calls, config, plan_admission, vendor_call  # noqa: E402

failed = []


def check(label, condition, extra=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}{(' — ' + extra) if extra and not condition else ''}")
    if not condition:
        failed.append(label)


def row(call_id):
    con = sqlite3.connect(config.db_path())
    con.row_factory = sqlite3.Row
    found = con.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    con.close()
    return dict(found) if found else {}


print("-- a capped $0 plan redirects only to a confirmed READY lane, otherwise refuses --")
orig_headroom = plan_admission.lanes.lane_headroom
orig_status = plan_admission.lanes.lanes_status
orig_overage = plan_admission.overage.current_overage_status
orig_lane_risk = plan_admission.lane_risk
orig_subs = None
from spendguard import lane_balance  # noqa: E402
orig_subs = lane_balance.substitutes_for
try:
    plan_admission.lanes.lane_headroom = lambda do_fetch=False: [
        {"lane": "claude-code", "provider": "anthropic", "known": False, "remaining_pct": None},
        {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 80.0},
    ]
    plan_admission.lanes.lanes_status = lambda: {"lanes": [
        {"lane": "claude-code", "enabled": True, "auth": "ok"},
        {"lane": "codex", "enabled": True, "auth": "ok"},
    ]}
    plan_admission.overage.current_overage_status = lambda: {"on_overage_now": True}
    plan_admission.lane_risk = lambda lane: {
        "lane": lane, "remaining_pct": None, "known": False, "capped": False,
        "paid_overage": lane == "claude-code", "at_risk": lane == "claude-code"}
    lane_balance.substitutes_for = lambda _intent: ["openai:gpt-6.1-sol"]
    decision = plan_admission.decide("anthropic:claude-opus-4-8", "test:intent")
    check("paid-overage call redirects off its plan", decision.get("action") == "redirect")
    check("decision names requested model, served model, lane, and reason",
          decision.get("requested_model") == "anthropic:claude-opus-4-8"
          and decision.get("model") == "openai:gpt-6.1-sol" and decision.get("lane") == "codex"
          and "overage" in decision.get("reason", ""), extra=str(decision))
    plan_admission.lanes.lanes_status = lambda: {"lanes": [
        {"lane": "claude-code", "enabled": True, "auth": "ok"},
        {"lane": "codex", "enabled": True, "auth": "missing"},
    ]}
    refused = plan_admission.decide("anthropic:claude-opus-4-8", "test:intent")
    check("no READY substitute refuses instead of failing open onto capped plan", refused.get("action") == "refuse")
finally:
    plan_admission.lanes.lane_headroom = orig_headroom
    plan_admission.lanes.lanes_status = orig_status
    plan_admission.overage.current_overage_status = orig_overage
    plan_admission.lane_risk = orig_lane_risk
    lane_balance.substitutes_for = orig_subs


print("-- the generic adapters.call redirect writes complete durable provenance --")
orig_decide = plan_admission.decide
orig_guarded = adapters._call_guarded
orig_served_substitute = vendor_call.served_substitute
seen_models = []
try:
    plan_admission.decide = lambda model, intent: {
        "action": "redirect", "requested_model": model, "model": "openai:gpt-6.1-sol",
        "lane": "codex", "reason": "plan paid-overage admission"}
    vendor_call.served_substitute = lambda _provider, model: (model, None)

    def fake_guarded(model, _prompt, **_kwargs):
        seen_models.append(model)
        cid = calls.record_call("openai", model.split(":", 1)[-1], "subscription", 0.0,
                                in_tok=17, out_tok=3, executor="codex", disposition="served")
        return {"provider": "openai", "model": model.split(":", 1)[-1], "text": "ok", "parsed": None,
                "in_tok": 17, "out_tok": 3, "cost": 0.0, "latency": 0.01, "finish_reason": "stop",
                "executor": "codex", "call_id": cid, "error": None}

    adapters._call_guarded = fake_guarded
    result = adapters.call("anthropic:claude-opus-4-8", "offline", intent="test:plan-redirect")
    persisted = row(result["call_id"])
    check("generic path served the redirect target", seen_models == ["openai:gpt-6.1-sol"])
    check("redirect result remains transparent", result.get("substituted_from") == "anthropic:claude-opus-4-8"
          and result.get("resolved_lane") == "codex")
    check("ledger row has requested model, served model, resolved lane, and reason",
          persisted.get("requested_model") == "anthropic:claude-opus-4-8"
          and persisted.get("served_model") == "gpt-6.1-sol" and persisted.get("resolved_lane") == "codex"
          and persisted.get("redirect_reason") == "plan paid-overage admission", extra=str(persisted))
finally:
    plan_admission.decide = orig_decide
    adapters._call_guarded = orig_guarded
    vendor_call.served_substitute = orig_served_substitute


print("-- caller pins remain hard and bypass plan substitution --")
decide_calls = []
orig_decide = plan_admission.decide
orig_guarded = adapters._call_guarded
orig_served_substitute = vendor_call.served_substitute
try:
    plan_admission.decide = lambda model, intent: decide_calls.append((model, intent)) or {"action": "refuse"}
    vendor_call.served_substitute = lambda _provider, model: (model, None)
    adapters._call_guarded = lambda model, _prompt, **_kwargs: {
        "provider": "anthropic", "model": model, "text": "pinned", "error": None, "cost": 0.0,
        "in_tok": 1, "out_tok": 1, "executor": "claude-code"}
    pinned = adapters.call("anthropic:claude-opus-4-8", "offline", intent="test:pinned",
                           no_substitution=True)
    check("explicit no_substitution never enters plan substitution", decide_calls == [] and pinned.get("text") == "pinned")
finally:
    plan_admission.decide = orig_decide
    adapters._call_guarded = orig_guarded
    vendor_call.served_substitute = orig_served_substitute


print("-- provenance and the real caller survive vendor_call's daemon thread --")
orig_call = adapters.call
thread_call_id = {}
try:
    def fake_adapter_call(model, _prompt, **_kwargs):
        thread_call_id["id"] = calls.record_call("openai", model.split(":", 1)[-1], "subscription", 0.0,
                                                  executor="codex", disposition="served")
        return {"model": model, "text": "ok", "error": None}

    adapters.call = fake_adapter_call
    with calls.redirect_context("anthropic:claude-opus-4-8", "load-balance: measured arm", "codex"):
        vendor_call._attempt("openai", "gpt-6.1-sol", "offline", None, 8, 2.0)
    threaded = row(thread_call_id["id"])
    check("daemon row preserves redirect provenance", threaded.get("requested_model") == "anthropic:claude-opus-4-8"
          and threaded.get("redirect_reason") == "load-balance: measured arm"
          and threaded.get("resolved_lane") == "codex")
    check("daemon row attributes the originating test frame, never thread.py:run",
          "test_plan_admission_and_redirect_provenance.py" in (threaded.get("caller") or "")
          and "thread.py:run" not in (threaded.get("caller") or ""), extra=str(threaded.get("caller")))
finally:
    adapters.call = orig_call

if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
print("ok — plan admission and redirect provenance are fail-closed, durable, and attributable")
