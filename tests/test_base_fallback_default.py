"""Tier-3 provider-base fallback is ON by default (Ash 2026-09-25: "provider always correct, user gets a result if at
all possible"). When a chosen model's lane AND metered API both fail, a NORMAL call falls back to that provider's
reliable BASE model (from the catalog SSOT) and yields SOME answer — but a PINNED/measurement call (no_substitution /
metered_only, where the model IS the measurement) does NOT: it relies on transient retry + a measured deadline
instead, so a consensus panel/judge is never silently moved to a different model. Offline: _call_guarded is stubbed."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-basefb-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"          # keep the test on the plain path (no durable-record hop)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


print("-- provider_base_model resolves from the catalog SSOT (no config override in this env) --")
ck("openai base = gpt-5.6-luna", adapters.provider_base_model("openai") == "gpt-5.6-luna")
ck("moonshot base = kimi-k3 (kimi always k3+)", adapters.provider_base_model("moonshot") == "kimi-k3")
ck("a provider with no base → None (tier-3 is a no-op for it)", adapters.provider_base_model("custom") is None)

# stub the guarded send: the openai BASE model (…luna) succeeds; every other model errors (lane+metered both failed)
_seen = []
_real_guarded = adapters._call_guarded
def _fake_guarded(model, prompt, **kw):
    _seen.append(model)
    if "luna" in model:
        return {"provider": "openai", "model": model, "text": "OK", "error": None, "cost": 0.001,
                "in_tok": 10, "out_tok": 2, "latency": 0.1, "finish_reason": "stop", "truncated": False}
    return {"provider": "openai", "model": model, "text": None, "error": "primary boom (lane+metered failed)",
            "error_type": "APIError", "cost": None, "in_tok": 0, "out_tok": 0, "latency": 0.0,
            "finish_reason": None, "truncated": None}


adapters._call_guarded = _fake_guarded
adapters._resolve_guard.on = True                          # skip the served-substitute resolver (deterministic id)
try:
    print("-- a NORMAL call: base_fallback auto-ON → tier-3 runs the provider base → a result --")
    _seen.clear()
    r = adapters.call("openai:gpt-5.5", "p")               # no intent/sig → no route/management hops
    ck("caller gets a RESULT (not the primary's error)", r.get("error") is None and r.get("text") == "OK")
    ck("the provider BASE (luna) ran after the primary failed", any("luna" in m for m in _seen))
    ck("tier-3 is LABELLED (base_fallback + substituted_from)",
       r.get("base_fallback") is True and r.get("substituted_from") == "openai:gpt-5.5")

    print("-- a PINNED call (no_substitution): base_fallback auto-OFF → NO tier-3 → honest error --")
    _seen.clear()
    r2 = adapters.call("openai:gpt-5.5", "p", no_substitution=True)
    ck("pinned: NO base fallback (the model is the measurement)", bool(r2.get("error")) and not r2.get("base_fallback"))
    ck("pinned: the base model was NOT run", not any("luna" in m for m in _seen))

    print("-- metered_only also pins the model → base_fallback OFF --")
    _seen.clear()
    r3 = adapters.call("openai:gpt-5.5", "p", metered_only=True)
    ck("metered_only: NO base fallback", bool(r3.get("error")) and not r3.get("base_fallback"))

    print("-- an EXPLICIT base_fallback=True on a pinned call still wins (caller's explicit choice) --")
    _seen.clear()
    r4 = adapters.call("openai:gpt-5.5", "p", no_substitution=True, base_fallback=True)
    ck("explicit base_fallback=True overrides the pin-off default", r4.get("text") == "OK" and r4.get("base_fallback") is True)
finally:
    adapters._call_guarded = _real_guarded
    adapters._resolve_guard.on = False

print(f"\n{'[FAIL]' if _fails else 'OK'} test_base_fallback_default: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
