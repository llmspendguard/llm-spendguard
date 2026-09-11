"""Guard — CONSENSUS-fan panel members get their learned effort AUTO-APPLIED, with diversity intact.

The "pending wire" (forwarding reasoning through vendor_call.fan_out) turns out to be unnecessary for the effort
axis: effort-titration auto-applies at the adapters._call_guarded chokepoint for ANY call carrying the intent, and
a consensus-fan member reaches that same chokepoint (fan_out → vendor_call.call → adapters.call → _call_guarded),
with _attempt propagating the caller's intent across the worker-thread boundary. So each member runs at its OWN
learned effort while keeping its OWN model — the panel gets best-value effort per member without any model swap
(diversity by construction; swapping a member's model is exactly the collapse we avoid).

Pins:
  · fan_out members each get their per-(intent,model) learned effort, applied at the chokepoint (no fan_out change)
  · each member keeps its own vendor/model (no collapse)
  · a member with no learned effort runs at its family floor (reasoning stays None → the family default downstream)
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import vendor_call, adapters, models, calls

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


INTENT = "t:panelfan"
# Each vendor slot has its own learned cheapest-holding effort; the third member is unmeasured (family floor).
models.record_effort("openai:gpt-5.5", INTENT, "low")
models.record_effort("anthropic:claude-opus-4-8", INTENT, "medium")
PANEL = [("openai", "gpt-5.5"), ("anthropic", "claude-opus-4-8"), ("zai", "glm-5.2")]

seen = {}
_orig_once = adapters._call_once
def _stub_once(model, prompt, max_tokens=None, system=None, reasoning=None, **kw):
    # capture what reached the wire AFTER the chokepoint auto-apply; keyed by the full vendor:model id
    seen[model] = reasoning
    prov = model.split(":", 1)[0] if ":" in model else "openai"
    return {"provider": prov, "model": model.split(":", 1)[-1], "text": "ok", "in_tok": 5, "out_tok": 3,
            "cost": 0.001, "latency": 0.0, "finish_reason": "stop", "error": None, "executor": "api"}
adapters._call_once = _stub_once
try:
    with calls.context(intent=INTENT):                       # the panel runs under its intent (as honestreview/crossllm do)
        fan = vendor_call.fan_out(PANEL, "review this", deadline_s=30, purpose=INTENT)
finally:
    adapters._call_once = _orig_once

print("-- every panel member reached the wire (distinct vendors, no collapse) --")
check("all three members were called", set(seen) == {"openai:gpt-5.5", "anthropic:claude-opus-4-8", "zai:glm-5.2"})
check("fan_out reports all three answered", fan.get("n") == 3 and fan.get("n_ok") == 3)

print("-- each member auto-applied ITS OWN learned effort (no fan_out change needed) --")
check("gpt-5.5 member ran at its learned 'low'", seen.get("openai:gpt-5.5") == "low")
check("opus member ran at its learned 'medium'", seen.get("anthropic:claude-opus-4-8") == "medium")

print("-- an unmeasured member runs at its family floor (reasoning stays None here) --")
check("glm member (no fact) got no forced effort", seen.get("zai:glm-5.2") is None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_panel_effort_autoapply: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
