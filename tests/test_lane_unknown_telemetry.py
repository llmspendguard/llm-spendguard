"""Unknown ledger telemetry cannot acquire a measured subscription value."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from spendguard import lane_catalog, lane_value, receipt


def test_lane_unknown_telemetry_preserves_unknown_value():
    lane_name = next(iter(lane_value.ledger_valued_lanes()))
    ledger_day = receipt._utc_today().isoformat()
    subscription_row = (lane_name, ledger_day, "provider", "model", 1, None, "project")
    with tempfile.TemporaryDirectory() as temporary_cache:
        with patch.object(lane_value, "_subscription_rows", return_value=[subscription_row]), \
                patch.object(receipt, "_receipt_aggregate_cache_path", return_value=Path(temporary_cache) / "cache.json"), \
                patch.object(lane_catalog, "use_name_cost", return_value=1) as unknown_lane_pricer:
            assert lane_value.stamp_from_ledger()[lane_name] is None
            pushed_row, = lane_value.day_totals("member")
            assert pushed_row["out_tokens"] is None
            assert pushed_row["spend_micros"] is None
            assert pushed_row["token_usage_complete"] is False
            assert pushed_row["value_complete"] is False
            unknown_lane_pricer.assert_not_called()

            known_subscription_row = (*subscription_row[:5], 1, subscription_row[-1])
            with patch.object(lane_value, "_subscription_rows", return_value=[known_subscription_row, subscription_row]):
                mixed_row, = lane_value.day_totals("member")
                assert mixed_row["calls"] == 2
                assert mixed_row["out_tokens"] is None
                assert mixed_row["spend_micros"] is None
                assert mixed_row["in_tokens"] == 2


if __name__ == "__main__":
    test_lane_unknown_telemetry_preserves_unknown_value()
