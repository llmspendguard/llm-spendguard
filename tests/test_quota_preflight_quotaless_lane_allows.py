"""A quota-less subscription lane remains usable while retaining preflight visibility."""
import sys

from spendguard import lane_quota_cost as quota

QUOTALESS_LANE = "gemini"
QUOTALESS_FAN_SIZE = quota.UNMEASURED_FANOUT_REFUSE_SIZE + 1

original_economics_for_quotaless_guard = quota.lane_economics.economics
original_caps_for_quotaless_guard = quota.lane_economics.measured_lane_caps
emitted_quotaless_statements = []

try:
    quota.lane_economics.economics = lambda headroom_rows=None: [
        {"lane": QUOTALESS_LANE, "binding": {"cap": None, "eff_usd_per_tok": None}},
    ]
    quota.lane_economics.measured_lane_caps = lambda _lane: []
    quotaless_result = quota.quota_preflight(
        {QUOTALESS_LANE: QUOTALESS_FAN_SIZE},
        headroom_rows=[{
            "lane": QUOTALESS_LANE,
            "known": False,
            "remaining_pct": None,
            "buckets": [],
        }],
        token_costs={},
        emit=emitted_quotaless_statements.append,
    )
finally:
    quota.lane_economics.economics = original_economics_for_quotaless_guard
    quota.lane_economics.measured_lane_caps = original_caps_for_quotaless_guard

quotaless_guard_passed = (
    quotaless_result["ok"]
    and not quotaless_result["refusals"]
    and len(emitted_quotaless_statements) == 1
    and f"lane={QUOTALESS_LANE}" in emitted_quotaless_statements[0]
    and "est_tokens=None" in emitted_quotaless_statements[0]
    and "effective_token_usd=None" in emitted_quotaless_statements[0]
)
print(("[OK]" if quotaless_guard_passed else "[FAIL]")
      + " quota-less known=False fan is allowed and emits its per-lane statement")
sys.exit(0 if quotaless_guard_passed else 1)
