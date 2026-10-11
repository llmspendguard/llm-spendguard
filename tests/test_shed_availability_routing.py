"""Shedding a capped/shed-from plan's SUBSTITUTABLE work is AVAILABILITY-based, not learning-gated, and works for ANY
provider. Measured 2026-10-10: a capped claude-code plan + an unproven intent returned "no confirmed READY substitute"
(the call ERRORED) because plan_admission.decide() only redirected to the pre-confirmed substitute registry, and
_ready() excluded every lane whose CLI has no status probe (gemini/zai/kimi read auth='unknown'). This locks in the
proper fix:
  • _ready(): OPTIMISTIC auth — 'ok' AND 'unknown' are try-able; only confirmed-down/missing is excluded.
  • decide(): a proven substitute is PREFERRED, but with none, a substitutable call is SERVED on the best ready
    ALTERNATIVE lane (availability core) rather than refused; refuse ONLY when nothing ready can serve.
  • advisor.shed_lanes: PROACTIVE shed-from (any provider) — a listed lane is at-risk even when not capped.
  • plan_admission.availability_fallback=off restores the old proven-only posture.
Pinned/measurement calls never reach decide() (the not-caller-pinned gate lives in adapters.call). Offline, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-shed-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import plan_admission as pa, adapters, lane_balance, lanes, config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# ── A. _ready() is OPTIMISTIC: 'ok' and 'unknown' are try-able; 'loggedout'/'missing' are not ──
lanes.lanes_status = lambda: {"lanes": [
    {"lane": "codex", "enabled": True, "auth": "ok"},
    {"lane": "gemini", "enabled": True, "auth": "unknown"},     # no status probe → unknown, must be try-able
    {"lane": "zai-coding", "enabled": True, "auth": "unknown"},
    {"lane": "down-lane", "enabled": True, "auth": "loggedout"},
    {"lane": "nocli-lane", "enabled": True, "auth": "missing"},
    {"lane": "disabled-lane", "enabled": False, "auth": "ok"},
]}
pa.lane_risk = lambda lane: {"lane": lane, "at_risk": False, "paid_overage": False, "proactive_shed": False,
                             "remaining_pct": None}
ck("_ready: auth='ok' lane is ready", pa._ready("codex") is True)
ck("_ready: auth='unknown' lanes (gemini/zai) are ready (OPTIMISTIC — the fix)",
   pa._ready("gemini") is True and pa._ready("zai-coding") is True)
ck("_ready: confirmed-down lane is NOT ready", pa._ready("down-lane") is False)
ck("_ready: missing-CLI lane is NOT ready", pa._ready("nocli-lane") is False)
ck("_ready: disabled lane is NOT ready", pa._ready("disabled-lane") is False)

# ── set up decide(): anthropic primary is AT-RISK, no proven substitute exists ──
adapters.provider_for = lambda m: "anthropic" if "claude" in (m or "") else "openai"
adapters._LANES = {"anthropic": ("claude-code",), "openai": ("codex",)}
lane_balance.substitutes_for = lambda intent: []                # NO pre-confirmed substitute for this intent
_RISK_ATRISK = {"lane": "claude-code", "at_risk": True, "paid_overage": True, "proactive_shed": False,
                "remaining_pct": 0.0}
pa.lane_risk = lambda lane: _RISK_ATRISK if lane == "claude-code" else {"lane": lane, "at_risk": False,
                                                                        "paid_overage": False, "proactive_shed": False}

# ── B. decide(): no proven substitute → SERVE on the best ready alternative lane (not refuse) ──
pa._best_ready_alt_lane = lambda primary, tier="cheap": ("gemini", "gemini:gemini-3-flash")
d = pa.decide("anthropic:claude-haiku-4-5", "unproven-intent-xyz")
ck("decide: substitutable + capped + no proven sub → REDIRECT (availability fallback), not refuse",
   d.get("action") == "redirect" and d.get("lane") == "gemini" and d.get("model") == "gemini:gemini-3-flash")

# ── C. decide(): a truly CAPPED lane with no ready alternative → refuse (cannot serve) ──
pa._best_ready_alt_lane = lambda primary, tier="cheap": None
d2 = pa.decide("anthropic:claude-haiku-4-5", "unproven-intent-xyz")
ck("decide: CAPPED + no proven sub + no ready alternative → refuse (only then)", d2.get("action") == "refuse")

# ── C2. a PROACTIVELY-shed-only lane (not capped) with no alternative → KEEP (it's available; shedding is a preference) ──
pa.lane_risk = lambda lane: ({"lane": lane, "at_risk": True, "capped": False, "paid_overage": False,
                              "proactive_shed": True, "remaining_pct": None} if lane == "claude-code"
                             else {"lane": lane, "at_risk": False, "paid_overage": False, "proactive_shed": False})
pa._best_ready_alt_lane = lambda primary, tier="cheap": None
d_keep = pa.decide("anthropic:claude-haiku-4-5", "unproven-intent-xyz")
ck("decide: proactively-shed (not capped) + no alternative → KEEP (not refuse)", d_keep.get("action") == "keep")
# restore the capped risk for the remaining sub-tests
pa.lane_risk = lambda lane: _RISK_ATRISK if lane == "claude-code" else {"lane": lane, "at_risk": False,
                                                                        "paid_overage": False, "proactive_shed": False}

# ── D. availability_fallback=off → old conservative refuse even when an alternative is ready ──
config.update_json(config.CONFIG_JSON, lambda c: {**c, "plan_admission": {"availability_fallback": False}},
                   reason="test: disable availability fallback")
pa._best_ready_alt_lane = lambda primary, tier="cheap": ("gemini", "gemini:gemini-3-flash")
d3 = pa.decide("anthropic:claude-haiku-4-5", "unproven-intent-xyz")
ck("decide: availability_fallback=off restores proven-only refuse", d3.get("action") == "refuse")
config.update_json(config.CONFIG_JSON, lambda c: {**c, "plan_admission": {"availability_fallback": True}},
                   reason="test: re-enable")

# ── E. PROACTIVE shed (advisor.shed_lanes): a listed lane is at-risk even when NOT capped ──
import importlib  # noqa: E402
importlib.reload(pa)   # restore the real lane_risk/_ready (we monkeypatched them above)
config.update_json(config.CONFIG_JSON, lambda c: {**c, "advisor": {"shed_lanes": "claude-code"}},
                   reason="test: proactive shed claude-code")
# a lane with no headroom row = not capped; proactive_shed should still mark it at-risk
lanes.lane_headroom = lambda do_fetch=False: []
risk = pa.lane_risk("claude-code")
ck("proactive shed: a shed_lanes lane is at_risk even when not capped", risk["at_risk"] is True and risk["proactive_shed"] is True)
risk_other = pa.lane_risk("codex")
ck("proactive shed: a NON-listed, non-capped lane is NOT at_risk", risk_other["at_risk"] is False)

print(f"\n{'[FAIL]' if fails else 'OK'} test_shed_availability_routing: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
