"""Phase-2C lane-usage status snapshot: bounded, scrubbed, and offline. Script-style."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-push-lane-usage-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import guard, lanes, overage, pricing, saas, setup  # noqa: E402

fails = []


def ck(label, condition):
    if not condition:
        fails.append(label)
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


originals = (lanes.lane_headroom, saas.saas_connection, saas.contributor_ok, saas.contributor, setup._probe,
             pricing.cross_check_openrouter, guard.shed_summary, guard.decision_rollup,
             overage.current_overage_status)
try:
    saas.saas_connection = lambda: {"visibility": "team"}
    saas.contributor_ok = lambda: (True, "ok")
    saas.contributor = lambda: "member"
    setup._probe = lambda executable: ("3", True, True)
    pricing.cross_check_openrouter = lambda: ([], None, None)
    guard.shed_summary = lambda: {"n_sheds": 0}
    guard.decision_rollup = lambda: {"n": 0, "by_route": []}
    overage.current_overage_status = lambda: {
        "on_overage_now": False, "observed_overage_windows": 0, "real_overage_by_month_usd": {}}
    rows = [{"lane": f"lane-{i}", "provider": "provider", "known": i != 2,
             "remaining_pct": (0.0, 100.0, None)[i % 3], "reset_ts": None,
             "remaining_abs": i * 10, "buckets": [{"private": "never push"}]} for i in range(18)]
    calls = []
    lanes.lane_headroom = lambda do_fetch=True: calls.append(do_fetch) or rows
    payload = saas.push_status(dry=True)
    snapshot = payload["lane_usage"]
    ck("cached headroom requested without provider fetch", calls == [False])
    ck("lane snapshot is capped at 16", len(snapshot["by_lane"]) == 16)
    ck("raw provider buckets are omitted", all("buckets" not in row for row in snapshot["by_lane"]))
    ck("remaining percent bounds and null are preserved",
       [row["remaining_pct"] for row in snapshot["by_lane"][:3]] == [0.0, 100.0, None])
    ck("snapshot has an ISO8601 UTC asof", snapshot["asof"].endswith("+00:00"))

    lanes.lane_headroom = lambda do_fetch=True: []
    empty = saas.push_status(dry=True)
    ck("empty lane snapshot is omitted", "lane_usage" not in empty)
finally:
    (lanes.lane_headroom, saas.saas_connection, saas.contributor_ok, saas.contributor, setup._probe,
     pricing.cross_check_openrouter, guard.shed_summary, guard.decision_rollup,
     overage.current_overage_status) = originals

print(f"\n{'[FAIL]' if fails else 'OK'} test_push_lane_usage: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
