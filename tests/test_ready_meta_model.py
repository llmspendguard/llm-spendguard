"""plan_admission.ready_meta_model: spendguard's OWN meta/discretionary calls (the batchable judge, the resolver,
best-value synthesis) must ride a READY provider, never be refused because one plan is capped. This is the general
fix behind the lane-eligibility judge going dark under claude-code paid-overage. Offline — the estate (providers,
lanes, per-lane models, readiness, ranking) is stubbed with declared fixtures; zero spend.

Contract pinned: healthy default plan → keep the default; capped default + a ready alternative → route to it
(provider-agnostic, because a meta judgement's model is not a measurement); capped default + NO ready alternative →
degrade to the default rather than refuse (a meta judgement must still run)."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-readymeta-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import plan_admission as pa, adapters, lane_catalog, route_utility, lanes

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# Stub the estate (declared fixtures, not logic): 3 providers/lanes, each lane's cheap model, provider, flat ranking.
adapters.provider_for = lambda m: {"claude-haiku-4-5": "anthropic", "gpt-5.6-luna": "openai", "glm-5.3": "zai"}.get(m, "anthropic")
adapters._LANES = {"anthropic": ("claude-code", "claudecode"), "openai": ("codex", "codex_exec"), "zai": ("zai-coding", "zai_exec")}
lane_catalog.lanes = lambda: ["claude-code", "codex", "zai-coding"]
lane_catalog.lane_model_for_tier = lambda lane, tier: {"codex": "gpt-5.6-luna", "zai-coding": "glm-5.3", "claude-code": "claude-haiku-4-5"}.get(lane)
lane_catalog.configured_base = lambda lane: None
lane_catalog.lane_provider = lambda lane: {"codex": "openai", "zai-coding": "zai", "claude-code": "anthropic"}.get(lane)
lanes.lane_headroom = lambda do_fetch=False: []
route_utility.rank_lanes = lambda rows, **k: [dict(r, available=True) for r in rows]   # flat: every candidate available, in order

# 1. the default's own plan is healthy → keep the configured default (nothing to change)
pa.lane_risk = lambda lane: {"at_risk": False}
ck("healthy default plan → keeps the configured meta model", pa.ready_meta_model("claude-haiku-4-5") == "claude-haiku-4-5")

# 2. the default's plan is capped, other lanes are READY → route to a ready provider's model (never refuse)
_AT_RISK = {"claude-code"}            # fixture: which lanes are at-risk
_READY = {"codex", "zai-coding"}      # fixture: which lanes are ready
pa.lane_risk = lambda lane: {"at_risk": lane in _AT_RISK, "paid_overage": True, "remaining_pct": 0}
pa._ready = lambda lane: lane in _READY
pick = pa.ready_meta_model("claude-haiku-4-5")
ck("capped default → routes to a READY provider's model", pick in {"gpt-5.6-luna", "glm-5.3"})
ck("the pick is NOT the capped plan's model", pick != "claude-haiku-4-5")

# 3. capped default and NO ready alternative → degrade to the default (a meta judgement must still run, never refuse)
pa._ready = lambda lane: False
ck("capped default + no ready alternative → degrade to default (never refuse)",
   pa.ready_meta_model("claude-haiku-4-5") == "claude-haiku-4-5")

print(("[OK]" if not fails else "[FAIL]") + " ready meta model: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
