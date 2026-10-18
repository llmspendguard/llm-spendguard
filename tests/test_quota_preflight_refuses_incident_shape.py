"""Offline acceptance checks for measured subscription-quota preflight."""
import os
import sys

from spendguard import lane_quota_cost as quota

INCIDENT_LANE = "claude-code"
ALTERNATE_LANE = "codex"
INCIDENT_CALL_COUNT = 12
INCIDENT_HEADROOM_PCT = 47.0
INCIDENT_TOKENS_PER_CALL = 40_000.0
MEASURED_CAP_TOKENS = 1_000_000.0
SMALL_CALL_COUNT = 1
SMALL_TOKENS_PER_CALL = 1_000.0
OVER_LIMIT_CALL_TOKENS = 60_000.0
MAX_FRACTION_ENV = "SPENDGUARD_MAX_PLAN_FRACTION"
AVOIDANCE_ENV = "SPENDGUARD_AVOID_CLAUDE_LANE"

fails = []


def check(label, condition):
    print(("[OK] " if condition else "[FAIL] ") + label)
    if not condition:
        fails.append(label)


def expect_refusal(label, function, required_fragments=()):
    try:
        function()
    except quota.PlanQuotaRefusal as exc:
        message = str(exc)
        check(label, all(fragment in message for fragment in required_fragments))
        return message
    check(label, False)
    return ""


def economics_fixture(headroom_rows=None):
    rows = []
    for headroom in headroom_rows or []:
        lane = headroom["lane"]
        rows.append({
            "lane": lane,
            "binding": {
                "cap": MEASURED_CAP_TOKENS,
                "eff_usd_per_tok": None,
            },
        })
    return rows


original_quota_snapshot_reader = quota.lanes.lane_headroom
quota_snapshot_fetches = []
original_economics = quota.lane_economics.economics
original_anthropic_lanes = quota.anthropic_lane_names
original_max_fraction = os.environ.get(MAX_FRACTION_ENV)
original_avoidance = os.environ.get(AVOIDANCE_ENV)
try:
    quota.lane_economics.economics = economics_fixture
    os.environ[MAX_FRACTION_ENV] = str(quota.MAX_PLAN_FRACTION_DEFAULT)
    headroom = [{
        "lane": INCIDENT_LANE,
        "known": True,
        "remaining_pct": INCIDENT_HEADROOM_PCT,
        "buckets": [],
    }]
    incident_cost = {
        INCIDENT_LANE: {
            "tokens_per_call": INCIDENT_TOKENS_PER_CALL,
            "samples": quota.MIN_SAMPLES_FOR_MEASURED,
            "basis": "measured",
        },
    }
    expect_refusal(
        "incident-sized fan refuses and names lane, projected cap percent, and shortfall",
        lambda: quota.quota_preflight(
            {INCIDENT_LANE: INCIDENT_CALL_COUNT},
            headroom_rows=headroom,
            token_costs=incident_cost,
        ),
        (INCIDENT_LANE, "48.00% of cap", "headroom 47.00%", "shortfall 1.00%"),
    )

    emitted = []
    small_cost = {
        INCIDENT_LANE: {
            "tokens_per_call": SMALL_TOKENS_PER_CALL,
            "samples": quota.MIN_SAMPLES_FOR_MEASURED,
            "basis": "measured",
        },
    }
    allowed = quota.quota_preflight(
        {INCIDENT_LANE: SMALL_CALL_COUNT},
        headroom_rows=headroom,
        token_costs=small_cost,
        emit=emitted.append,
    )
    check("small fan allows and emits an auditable per-lane statement", allowed["ok"] and len(emitted) == 1)

    expect_refusal(
        "one oversized call refuses without authorization",
        lambda: quota.enforce_single_call_plan_fraction(
            INCIDENT_LANE,
            OVER_LIMIT_CALL_TOKENS,
            headroom_rows=headroom,
            token_costs=small_cost,
        ),
        (INCIDENT_LANE, "above"),
    )
    override = quota.enforce_single_call_plan_fraction(
        INCIDENT_LANE,
        OVER_LIMIT_CALL_TOKENS,
        auth_over_quota=True,
        headroom_rows=headroom,
        token_costs=small_cost,
    )
    check("one oversized call allows with explicit authorization", override["cap_tokens"] == MEASURED_CAP_TOKENS)

    # Default single-call enforcement must use the snapshot and still enforce its cap.
    quota.lanes.lane_headroom = lambda do_fetch=True: (quota_snapshot_fetches.append(do_fetch) or headroom)
    expect_refusal(
        "snapshot-backed oversized single call still refuses",
        lambda: quota.enforce_single_call_plan_fraction(
            INCIDENT_LANE, OVER_LIMIT_CALL_TOKENS, token_costs=small_cost),
        (INCIDENT_LANE, "above"),
    )
    check("single call never refreshes unrelated host lanes", quota_snapshot_fetches == [False])
    quota_snapshot_fetches.clear()
    quota.quota_preflight({INCIDENT_LANE: SMALL_CALL_COUNT}, token_costs=small_cost)
    check("explicit fan preflight retains live quota refresh", quota_snapshot_fetches == [True])
    quota.lanes.lane_headroom = original_quota_snapshot_reader

    fan_size = quota.UNMEASURED_FANOUT_REFUSE_SIZE + 1
    unmeasured_cost = {
        INCIDENT_LANE: {
            "tokens_per_call": SMALL_TOKENS_PER_CALL,
            "samples": quota.MIN_SAMPLES_FOR_MEASURED - 1,
            "basis": "unmeasured",
        },
    }
    expect_refusal(
        "unmeasured lane above the imported fan threshold refuses",
        lambda: quota.quota_preflight(
            {INCIDENT_LANE: fan_size},
            headroom_rows=headroom,
            token_costs=unmeasured_cost,
        ),
        ("unmeasured quota cost",),
    )
    measured_peer = quota.quota_preflight(
        {INCIDENT_LANE: fan_size},
        headroom_rows=headroom,
        token_costs=small_cost,
    )
    check("same-size measured lane is not refused on the unmeasured ground", measured_peer["ok"])

    quota.anthropic_lane_names = lambda: {INCIDENT_LANE}
    os.environ[AVOIDANCE_ENV] = "on"
    check(
        "avoidance on excludes Anthropic when another ready lane can serve",
        quota.apply_anthropic_avoidance([INCIDENT_LANE, ALTERNATE_LANE]) == [ALTERNATE_LANE],
    )
    os.environ[AVOIDANCE_ENV] = "off"
    check(
        "avoidance off restores Anthropic eligibility",
        quota.apply_anthropic_avoidance([INCIDENT_LANE, ALTERNATE_LANE]) == [INCIDENT_LANE, ALTERNATE_LANE],
    )
    os.environ[AVOIDANCE_ENV] = "on"
    expect_refusal(
        "sole-capable Anthropic lane refuses without authorization",
        lambda: quota.apply_anthropic_avoidance([INCIDENT_LANE]),
        ("sole ready+capable lane",),
    )
    check(
        "sole-capable Anthropic lane allows with explicit authorization",
        quota.apply_anthropic_avoidance([INCIDENT_LANE], auth_over_quota=True) == [INCIDENT_LANE],
    )
finally:
    quota.lanes.lane_headroom = original_quota_snapshot_reader
    quota.lane_economics.economics = original_economics
    quota.anthropic_lane_names = original_anthropic_lanes
    if original_max_fraction is None:
        os.environ.pop(MAX_FRACTION_ENV, None)
    else:
        os.environ[MAX_FRACTION_ENV] = original_max_fraction
    if original_avoidance is None:
        os.environ.pop(AVOIDANCE_ENV, None)
    else:
        os.environ[AVOIDANCE_ENV] = original_avoidance

print(f"\n{'[FAIL]' if fails else 'OK'} test_quota_preflight_refuses_incident_shape: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
