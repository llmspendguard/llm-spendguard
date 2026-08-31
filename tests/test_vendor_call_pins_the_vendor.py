"""vendor_call.call NAMES one vendor — so it must PIN that vendor. Its whole contract is "Call ONE model"; a
lane-bandit substitution that answers as a different vendor breaks it, and silently collapses any caller that is a
cross-vendor panel or a 2-judge adjudication (honestreview's review panels, warden-style consensus). So vendor_call
passes no_substitution=True to adapters.call on BOTH its paths (the attempt + the reachability probe). This locks
that in: a call named for a vendor can never be swapped for another. Offline, isolated, no network, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-vcpin-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import vendor_call as vc, adapters

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

captured = {}
_orig = adapters.call
def _cap(model, prompt, **kw):
    captured["model"] = model
    captured.update(kw)
    return {"text": "ok", "model": model, "provider": str(model).split(":")[0], "executor": "api",
            "error": None, "in_tok": 1, "out_tok": 1, "cost": 0.0, "finish_reason": "stop", "latency": 0.01}
adapters.call = _cap
try:
    vc.call("anthropic", "claude-opus-4-8", "hi", deadline_s=30, max_tokens=64, purpose="test:vc-pin")
    ck("vendor_call.call pins the vendor (no_substitution=True reaches adapters.call)",
       captured.get("no_substitution") is True)
    ck("...and it targeted exactly the NAMED vendor (never a swap)",
       str(captured.get("model", "")).startswith("anthropic"))
finally:
    adapters.call = _orig

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"vendor_call_pins_the_vendor: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
