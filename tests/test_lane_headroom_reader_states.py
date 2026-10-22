"""Structural quota state guards; offline reader faults must remain named unknowns."""
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import lane_economics, lane_quota_cost, lanes

KNOWN_PERCENT = 61
TEST_LANE = "structural-fixture"
TEST_PROVIDER = "fixture-provider"
HEADROOM_ESTIMATE_SAMPLES = 7
HEADROOM_ESTIMATE_WINDOW = "fixture-window"
HEADROOM_ESTIMATE_UNCERTAINTY = "fixture-uncertainty"
HEADROOM_INVALID_PERCENTAGES = (-1, 101, float("nan"), float("inf"), -float("inf"), True)


def exercise_headroom_reader_fixture(module):
    status = {"lanes": [{"lane": TEST_LANE, "provider": TEST_PROVIDER, "enabled": True}]}
    with patch.object(lanes, "_lane_mods", return_value={TEST_LANE: module}), \
         patch.object(lanes, "lanes_status", return_value=status), \
         patch.object(lanes.config, "save_state"), \
         patch.object(lane_economics, "record_samples"), \
         patch.object(lane_economics, "remaining_abs_by_lane", return_value={}):
        return lanes.lane_headroom()[0]


def render_headroom_reader_fixture(row):
    output = StringIO()
    with patch.object(lanes, "lane_headroom", return_value=[row]), \
         patch.object(lanes, "lane_summary_lines", return_value=["fixture"]), \
         patch.object(lane_quota_cost, "measured_lane_token_cost", return_value={}), \
         patch("spendguard.lane_eligibility.summary_line", return_value=None), redirect_stdout(output):
        lanes.main(["--usage"])
    return output.getvalue()


def raise_headroom_reader_fixture_fault():
    raise TimeoutError("injected reader timeout")


def assert_headroom_reader_state_guards():
    for module in (SimpleNamespace(), SimpleNamespace(usage=None)):
        absent = exercise_headroom_reader_fixture(module)
        assert absent["headroom_state"] == "no_reader" and not absent["known"]
        assert "no reader wired" in render_headroom_reader_fixture(absent)
    invalid_buckets = tuple([{"remaining_pct": percent}] for percent in HEADROOM_INVALID_PERCENTAGES)
    for returned in (None, [], [{"bucket": "partial"}], *invalid_buckets):
        unknown = exercise_headroom_reader_fixture(SimpleNamespace(usage=lambda returned=returned: returned))
        assert unknown["headroom_state"] == "reader_no_data" and not unknown["known"]
        assert unknown["remaining_pct"] is None
        assert unknown["buckets"] is None
        text = render_headroom_reader_fixture(unknown)
        assert "reader wired but no data this run" in text
        assert "no quota API" not in text and "available" not in text
    fault = exercise_headroom_reader_fixture(SimpleNamespace(usage=raise_headroom_reader_fixture_fault))
    assert fault["headroom_gap"] == "headroom_reader_error:TimeoutError"
    assert not fault["known"] and fault["remaining_pct"] is None
    report = exercise_headroom_reader_fixture(SimpleNamespace(usage_report=lambda: {
        "buckets": None, "reason": "fixture_missing_configuration", "source": "fixture-evidence"}))
    assert report["headroom_gap"] == "fixture_missing_configuration"
    assert "fixture-evidence" in render_headroom_reader_fixture(report)
    known = exercise_headroom_reader_fixture(SimpleNamespace(usage=lambda: [{"remaining_pct": KNOWN_PERCENT}]))
    assert known["headroom_state"] == "known" and known["known"]
    assert known["remaining_pct"] == KNOWN_PERCENT
    assert f"{KNOWN_PERCENT}% left" in render_headroom_reader_fixture(known)
    metadata = {"n": HEADROOM_ESTIMATE_SAMPLES, "window": HEADROOM_ESTIMATE_WINDOW,
                "uncertainty": HEADROOM_ESTIMATE_UNCERTAINTY}
    for measured_buckets in (None, [{"remaining_pct": KNOWN_PERCENT}]):
        estimate = exercise_headroom_reader_fixture(SimpleNamespace(usage_report=lambda buckets=measured_buckets: {
            "buckets": buckets, "source": "fixture-estimate", **metadata}))
        for key, value in metadata.items():
            assert estimate[f"headroom_{key}"] == value
        rendered_estimate = render_headroom_reader_fixture(estimate)
        assert "estimate" in rendered_estimate
        for key, value in metadata.items():
            assert f"{key}={value}" in rendered_estimate
        if measured_buckets is None:
            assert "available" not in rendered_estimate
    print("headroom reader guards: absent/noncallable, no-data/empty/partial/nonfinite, timeout fault, report, known PASS")


if __name__ == "__main__":
    assert_headroom_reader_state_guards()
