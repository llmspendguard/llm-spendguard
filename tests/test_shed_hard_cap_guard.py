"""Soft shedding must never re-admit a hard-capped lane. Offline, config-driven boundary guard."""
from unittest.mock import patch

from spendguard import config, lane_economics, lane_quota_cost, lanes, route_utility

SHED_CAP_LANE = "shed-cap-fixture"
OTHER_CAP_LANE = "other-cap-fixture"
CAP_PERCENT_SCALE = 100
CAP_RESERVE_CASES = (0.0, 0.25)
CAP_BOUNDARY_OFFSETS = (0, -1)
CAP_READY_PERCENT = 80
CAP_SOFT_PERCENT = 90
CAP_CONFIG_BASE = config._cfg_get

with (
    patch.object(config, "shed_lanes", return_value={SHED_CAP_LANE}),
    patch.object(lane_quota_cost, "measured_lane_token_cost", return_value={}),
    patch.object(lane_quota_cost, "avoid_anthropic_lane_enabled", return_value=False),
    patch.object(lane_economics, "pace_by_lane", return_value={}),
    patch.object(route_utility, "_protect_policy", return_value=lambda lane: False),
):
    for cap_reserve in CAP_RESERVE_CASES:
        with patch.object(config, "_cfg_get", side_effect=lambda section, key, default=None, cap_reserve=cap_reserve: (
            {SHED_CAP_LANE: {"reserve_frac": cap_reserve}, OTHER_CAP_LANE: {"reserve_frac": cap_reserve}}
            if (section, key) == ("subscription", "pace") else CAP_CONFIG_BASE(section, key, default)
        )):
            for cap_offset in CAP_BOUNDARY_OFFSETS:
                cap_rows = [
                    {"lane": SHED_CAP_LANE, "known": True,
                     "remaining_pct": cap_reserve * CAP_PERCENT_SCALE + cap_offset},
                    {"lane": OTHER_CAP_LANE, "known": True, "remaining_pct": CAP_READY_PERCENT},
                ]
                cap_ranked = route_utility.rank_lanes(cap_rows, cooling=lambda lane: False, pace_by={})
                assert [entry["lane"] for entry in cap_ranked if entry["available"]] == [OTHER_CAP_LANE]
                cap_entry = next(entry for entry in cap_ranked if entry["lane"] == SHED_CAP_LANE)
                # Loss of the alternative must not activate the soft-shed fallback on an exhausted lane.
                cap_rows.pop()
                cap_ranked = route_utility.rank_lanes(cap_rows, cooling=lambda lane: False, pace_by={})
                assert not any(entry["available"] for entry in cap_ranked), cap_ranked
                assert not cap_entry["available"] and "window cap" in cap_entry["why"]
                with patch.object(lanes, "lane_headroom", return_value=cap_rows):
                    cap_action, cap_target, cap_reason = route_utility.breach_decision(policy="downgrade")
                assert cap_action == "refuse" and cap_target is None and cap_reason
            # Preserve advisory semantics: a shed lane WITH capacity remains the free fallback.
            cap_soft_rows = [{"lane": SHED_CAP_LANE, "known": True, "remaining_pct": CAP_SOFT_PERCENT}]
            cap_soft_ranked = route_utility.rank_lanes(cap_soft_rows, cooling=lambda lane: False, pace_by={})
            assert cap_soft_ranked[0]["available"], cap_soft_ranked

print("[OK] shed_hard_cap_guard: configured cap beats soft preference; exhaustion refuses")
