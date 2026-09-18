"""Reliability guard: a SATURATED $0 subscription lane SHEDS to its metered twin instead of hard-failing — so a
burst behaves like isolation ("work consistently always").

The warden bakeoff exposed the bug: haiku ran as BOTH candidate and judge on the ONE claude-code lane; under a
64-call burst the lane's concurrency bucket filled, the overflow timed out in the governor, and vendor_call
returned DEADLINE_EXCEEDED with NO fallback — while a lane ERROR one layer down (adapters._call_once) already
fell back to metered. That INCONSISTENCY (isolation works 8/8, burst hard-fails) is the defect.

The fix (vendor_call.call + dispatch.acquire skip_lane): a lane-queue DispatchTimeout re-acquires under the
metered VENDOR key — a DIFFERENT governor bucket with its OWN cap, not the saturated lane — and runs metered,
the SAME destination a lane error reaches. Pins four properties that must never regress:
  1. a saturated $0 LANE sheds → the call COMPLETES on the metered twin (not a deadline), forcing metered_only;
  2. a saturated METERED vendor is real 429-protection with no cheaper twin → it STAYS a deadline (no shed);
  3. no_metered_fallback (the $0-only contract) → a lane miss is an honest failure, never a surprise charge;
  4. dispatch.acquire(skip_lane=True) keys on the VENDOR, not the lane (so the shed is not re-queued behind the
     very lane bucket it just timed out of).

Hermetic: in-process governor only (XP_OFF), adapters._lane_for + adapters.call stubbed — no network, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-shed-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"           # in-process semaphores only — deterministic, no flock/fs
os.environ["SPENDGUARD_DISPATCH_LANE_CONCURRENCY"] = "1"  # ONE lane slot → a single held acquire saturates it
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import dispatch, vendor_call, adapters   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── stubs: "anthropic" rides the claude-code $0 lane; "openai" is metered-only. No real model is ever touched ──
_LANE = ("claude-code", object())          # a truthy (lane_name, exec_module) — what _lane_for returns when active
_calls_seen = []                            # every adapters.call the attempt makes, with the metered_only it received


def _fake_lane_for(prov):
    return _LANE if prov == "anthropic" else None


def _fake_adapters_call(model, prompt, **kw):
    _calls_seen.append({"model": model, "metered_only": kw.get("metered_only"),
                        "no_metered_fallback": kw.get("no_metered_fallback")})
    return {"text": "served", "in_tok": 5, "out_tok": 3, "cost": 0.001, "finish_reason": "stop",
            "status_code": 200, "error": None}


adapters._lane_for = _fake_lane_for
adapters.call = _fake_adapters_call


# ── 4. dispatch keying: skip_lane forces the VENDOR key even for a lane vendor (the shed's whole premise) ──────
print("-- dispatch.acquire(skip_lane) keys on the metered vendor, not the lane --")
_k_lane = dispatch._GOV._key_and_limit("anthropic", "claude-haiku-4-5", skip_lane=False)[0]
_k_vend = dispatch._GOV._key_and_limit("anthropic", "claude-haiku-4-5", skip_lane=True)[0]
ck("skip_lane=False → the lane bucket (lane:claude-code)", _k_lane == "lane:claude-code")
ck("skip_lane=True → the metered vendor bucket (vendor:anthropic), a DIFFERENT cap", _k_vend == "vendor:anthropic")

# ── 5. the $0 default is intact: an UNSATURATED lane still serves the call on the lane (no needless metered) ───
print("\n-- an unsaturated $0 lane still serves the call (the shed does not fire when the lane is free) --")
_calls_seen.clear()
res = vendor_call.call("anthropic", "claude-haiku-4-5", "hi", deadline_s=5, max_tokens=100, purpose="test:free")
ck("a free lane serves the call", res.ok)
ck("...and it did NOT force metered (rode the $0 lane, metered_only=False)",
   len(_calls_seen) == 1 and _calls_seen[0]["metered_only"] is False)

# ── 1. a SATURATED $0 lane SHEDS to the metered twin — the call COMPLETES instead of timing out ───────────────
print("\n-- a SATURATED $0 lane sheds to metered (burst behaves like isolation) --")
_calls_seen.clear()
dispatch.acquire("anthropic", "claude-haiku-4-5", deadline_s=10)     # hold the ONLY claude-code lane slot
try:
    res = vendor_call.call("anthropic", "claude-haiku-4-5", "hi", deadline_s=0.6, max_tokens=100,
                           purpose="test:shed")
finally:
    dispatch.release("anthropic", "claude-haiku-4-5")
ck("a saturated $0 lane did NOT hard-fail with a deadline", res.kind != vendor_call.DEADLINE_EXCEEDED)
ck("...it SHED to metered and the call completed", res.ok)
ck("...and the shed forced the metered path (adapters.call metered_only=True)",
   len(_calls_seen) == 1 and _calls_seen[0]["metered_only"] is True)

# ── 3. no_metered_fallback is the $0-only contract: a saturated lane stays an HONEST failure, no metered charge ─
print("\n-- no_metered_fallback: a saturated lane is an honest deadline, never a surprise metered charge --")
_calls_seen.clear()
dispatch.acquire("anthropic", "claude-haiku-4-5", deadline_s=10)
try:
    res = vendor_call.call("anthropic", "claude-haiku-4-5", "hi", deadline_s=0.6, max_tokens=100,
                           purpose="test:refuse", no_metered_fallback=True)
finally:
    dispatch.release("anthropic", "claude-haiku-4-5")
ck("no_metered_fallback → the saturated lane is DEADLINE_EXCEEDED", res.kind == vendor_call.DEADLINE_EXCEEDED)
ck("...and NOTHING was billed to the metered API (adapters.call never reached)", len(_calls_seen) == 0)

# ── 2. a saturated METERED vendor has no cheaper twin → it STAYS a deadline (real 429-protection, no shed) ─────
print("\n-- a saturated METERED vendor stays a deadline (no lane to shed FROM, no cheaper twin to shed TO) --")
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY"] = "1"           # one openai slot
_calls_seen.clear()
dispatch.acquire("openai", "gpt-x", deadline_s=10)                    # hold the only vendor:openai slot
try:
    res = vendor_call.call("openai", "gpt-x", "hi", deadline_s=0.6, max_tokens=100, purpose="test:metered")
finally:
    dispatch.release("openai", "gpt-x")
    os.environ.pop("SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY", None)
ck("a saturated metered vendor STAYS DEADLINE_EXCEEDED (no shed)", res.kind == vendor_call.DEADLINE_EXCEEDED)
ck("...and it did not fire a phantom metered attempt", len(_calls_seen) == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_shed_to_metered: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
