"""The lane + metered reachability sweep — estimate-first ($0), then a tiny live probe per resource — must (a)
never spend on the estimate, (b) enumerate every configured lane + every KEYED metered provider, (c) build a
reachability matrix, and (d) derive a metered target without a blind hardcode
(config override → a model THIS install depends on → named default → cheapest-served).

Offline: adapters.call, the lane probe, config.api_key, and the catalog/pricing are stubbed — no network, no
spend. The load-bearing guarantee is ESTIMATE-FIRST: sweep(run=False) must make ZERO calls.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-reliab-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import (reliability, adapters, config, catalog, pricing, lanes, lane_catalog,   # noqa: E402
                        model_preflight)

fails = []
PROBE_TIMEOUT_SECONDS = 3
DIAGNOSTIC_REPEAT_COUNT = 40


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# Two keyed metered providers (openai, deepseek), the rest unkeyed; a live catalog + prices for the derivation.
_KEYED = {"openai", "deepseek"}
config.api_key = lambda name: "k" if any(name.startswith(p.upper()) for p in _KEYED) else None
catalog.live_model_ids = lambda prov: {"openai": ["gpt-5-nano", "babbage-002"], "deepseek": ["deepseek-v4-flash"]}.get(prov)
_PRICES = {"openai:gpt-5-nano": 0.4, "openai:babbage-002": 1.6, "deepseek:deepseek-v4-flash": 0.28}
pricing.price = lambda m: {"out": _PRICES.get(m)}
pricing.realtime_cost = lambda m, i, o: 0.00001
# `_metered_target` PREFERS a model this install actually depends on (model_preflight.configured_specs) over the
# catalog derivation. Stub it empty by DEFAULT so the derivation blocks below isolate the config→default→cheapest
# path; the dependency-preference layer gets its own assertion in block (d) with a temporary stub.
model_preflight.configured_specs = lambda: ()

# Count calls so we can PROVE the estimate spends nothing.
_calls = {"n": 0}


def _stub_call(model, prompt, **kw):
    _calls["n"] += 1
    # mirror adapters.call's contract for an api-served call: served_by_metered_api=True (set at adapters.call L697
    # from executor='api'). sweep now VERIFIES the raw metered key on this field, so the stub must carry it.
    return {"text": "ok", "error": None, "cost": 0.00001, "executor": "api", "served_by_metered_api": True}


adapters.call = _stub_call
lanes.probe = lambda timeout_s=None: [{"lane": "codex", "ok": True, "latency": 2.0}, {"lane": "gemini", "ok": False, "error": "quota"}]


print("-- (d) the metered target is derived, never a blind hardcode --")
config._cfg_get = (lambda orig: (lambda s, k, d=None: {} if (s, k) == ("reliability", "probe_models") else orig(s, k, d)))(config._cfg_get)
model_preflight.configured_specs = lambda: ("openai:gpt-5.6-sol",)   # a model this install DEPENDS on for openai
check("a configured openai dependency is PREFERRED over the derivation (verify the id you depend on)",
      reliability._metered_target("openai") == "gpt-5.6-sol")
model_preflight.configured_specs = lambda: ()                        # none configured → fall through to derivation
check("openai target = the named cheap default (gpt-5-nano, and it IS in the live catalog)", reliability._metered_target("openai") == "gpt-5-nano")
check("deepseek default ('deepseek-chat') absent → derived cheapest-served (deepseek-v4-flash, not babbage)",
      reliability._metered_target("deepseek") == "deepseek-v4-flash")

print("\n-- (b) plan enumerates every lane + every KEYED metered provider --")
pl = reliability.plan()
check("every registered lane is in the plan (derived, not a hardcoded set)",
      {l for l, _m in pl["lanes"]} == set(lane_catalog.lanes()))
check("only the keyed metered providers (openai, deepseek), unkeyed excluded", {p for p, _m in pl["metered"]} == _KEYED)

print("\n-- (a) ESTIMATE-FIRST: sweep(run=False) spends NOTHING --")
_calls["n"] = 0
out = reliability.sweep(run=False)
check("run=False makes ZERO calls (a pure estimate)", _calls["n"] == 0)
check("...and returns a metered $ estimate", out["estimate"]["metered_cost"] > 0 and out["estimate"]["n_metered"] == 2)

print("\n-- (c) sweep(run=True) probes each resource and builds the reachability matrix --")
_calls["n"] = 0
res = reliability.sweep(run=True)
check("one metered call per keyed provider", _calls["n"] == 2)
check("lane matrix from the $0 probe (codex reachable, gemini not)",
      res["lanes"]["codex"]["reachable"] is True and res["lanes"]["gemini"]["reachable"] is False)
check("metered matrix carries model + reachable + cost", res["metered"]["openai"]["reachable"] and res["metered"]["openai"]["model"] == "gpt-5-nano")

print("\n-- probe failures retain complete diagnostics and request shape --")
diagnostic_message = "unexpected request keyword " + ("detail" * DIAGNOSTIC_REPEAT_COUNT)


def _raise_probe_typeerror(*args, **kwargs):
    raise TypeError(diagnostic_message)


adapters.call = _raise_probe_typeerror
_provider, failed_probe = reliability._probe_metered_one("anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
check("TypeError repr is complete, including its untruncated message",
      repr(TypeError(diagnostic_message)) in failed_probe["reason"])
check("failure records request shape and traceback location",
      "request_shape=" in failed_probe["reason"] and "metered_only" in failed_probe["reason"]
      and "_raise_probe_typeerror" in failed_probe["reason"])

print("\n-- returned adapter failures distinguish a client bug from a provider outage --")
adapter_message = "invalid local argument " + ("evidence" * DIAGNOSTIC_REPEAT_COUNT)
adapters.call = lambda *args, **kwargs: {
    "error": adapter_message, "error_type": "TypeError", "error_module": "builtins",
    "error_origin": "adapters.py:3016 in _astream", "status_code": None, "executor": "api",
    "served_by_metered_api": False, "failure_kind": "client_bug",
}
_provider, client_bug_probe = reliability._probe_metered_one("anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
check("client-side TypeError is fail-closed but classified as adapter/client bug",
      client_bug_probe["reachable"] is False and client_bug_probe["failure_kind"] == "client_bug"
      and client_bug_probe["reason"].startswith("adapter/client bug: TypeError"))
check("client-bug diagnostic preserves the full message and originating frame",
      adapter_message in client_bug_probe["reason"] and "adapters.py:3016 in _astream" in client_bug_probe["reason"])

adapters.call = lambda *args, **kwargs: {
    "error": "service unavailable", "error_type": "APIStatusError", "status_code": 503,
    "executor": "api", "served_by_metered_api": False, "failure_kind": "provider_error",
}
_provider, outage_probe = reliability._probe_metered_one("anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
check("HTTP 5xx proves the provider was reached and is classified as provider error",
      outage_probe["reachable"] is False and outage_probe["failure_kind"] == "provider_error"
      and outage_probe["reason"].startswith("provider error:"))

print("\n-- statusless fallback uses structural origin and remains fail-closed --")
provider_origin = os.path.join(os.path.dirname(os.path.abspath(adapters.__file__)), "provider-origin.py")
provider_package_origin = os.path.join(os.path.dirname(os.path.abspath(__import__("anthropic").__file__)),
                                       "provider-origin.py")
unknown_origin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unknown-origin.py")
failure_cases = (
    (provider_package_origin, "TypeError", None, "indeterminate"),
    (provider_origin, "ValueError", None, "indeterminate"),
    (unknown_origin, "TypeError", None, "indeterminate"),
    (None, "JSONDecodeError", None, "indeterminate"),
    (None, "APIStatusError", 400, "client_bug"),
    (None, "APIStatusError", 404, "client_bug"),
    # HTTP 408 means the client did not complete its request within the server's wait window.
    (None, "APIStatusError", 408, "client_bug"),
    (None, "APIStatusError", 429, "provider_error"),
    (None, "APIStatusError", 503, "provider_error"),
    (None, "APIStatusError", 399, "indeterminate"),
)
for error_origin_path, error_type_name, status_code, expected_kind in failure_cases:
    adapters.call = lambda *args, _origin=error_origin_path, _error_type_name=error_type_name, \
            _status_code=status_code, **kwargs: {
        "error": "offline constructed failure", "error_type": _error_type_name, "status_code": _status_code,
        "error_origin": f"{_origin}:1 in probe" if _origin else None,
        "executor": "api", "served_by_metered_api": False,
    }
    _provider, structural_probe = reliability._probe_metered_one(
        "anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
    check(f"{error_type_name} at {error_origin_path!r} fallback is {expected_kind} and fail-closed",
          structural_probe["reachable"] is False and structural_probe["failure_kind"] == expected_kind)

adapters.call = lambda *args, **kwargs: {
    "error": "transport failed", "error_type": "ConnectionError", "status_code": None,
    "executor": "api", "served_by_metered_api": False, "failure_kind": "provider_unreachable",
}
_provider, transport_probe = reliability._probe_metered_one("anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
check("adapter-computed transport kind is preferred and remains fail-closed",
      transport_probe["reachable"] is False and transport_probe["failure_kind"] == "provider_unreachable")

adapters.call = lambda *args, **kwargs: {
    "error": "could not interpret local failure", "error_type": "ValueError", "status_code": None,
    "executor": "api", "served_by_metered_api": False,
}
_provider, indeterminate_probe = reliability._probe_metered_one("anthropic", "probe-model", PROBE_TIMEOUT_SECONDS)
check("indeterminate health reason names the attribution gap rather than an adapter/client bug",
      indeterminate_probe["reason"].startswith(
          "indeterminate failure (cannot attribute to provider vs client): ValueError"))

saved_health_reds = reliability.health_reds
try:
    reliability.health_reds = lambda: [{"resource": "anthropic", "failure_kind": "client_bug",
                                        "reason": client_bug_probe["reason"], "fix": None, "command": None}]
    check("health alert names adapter/client bugs without claiming the provider is unreachable",
          "adapter/client bug" in reliability.health_alert() and "resource(s) unreachable" not in reliability.health_alert())
finally:
    reliability.health_reds = saved_health_reds

try:
    reliability.health_reds = lambda: [{"resource": "anthropic", "failure_kind": "indeterminate",
                                        "reason": indeterminate_probe["reason"], "fix": None, "command": None}]
    check("health alert surfaces an indeterminate failure as not healthy and cannot attribute it",
          "not healthy" in reliability.health_alert() and "cannot attribute" in reliability.health_alert())
finally:
    reliability.health_reds = saved_health_reds

print(f"\n{'[FAIL]' if fails else 'OK'} test_reliability: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
