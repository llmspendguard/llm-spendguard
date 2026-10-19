"""Durable purge throttle survives pooled-connection loss; storage errors remain due."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix="sg-purge-throttle-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import lane_queue

THROTTLE_CLOCK_START = 10000.0
THROTTLE_INTERVAL_S = 100.0
THROTTLE_REOPEN_OFFSET_S = THROTTLE_INTERVAL_S / 2


def test_purge_throttle_survives_connection_restart():
    assert not hasattr(lane_queue, "_last_purge_ts")
    with patch("time.time", return_value=THROTTLE_CLOCK_START):
        result = lane_queue.purge()
    assert "error" not in result, result
    original_connection = lane_queue._queue_conn()
    lane_queue._reset_queue_conn()
    assert lane_queue._queue_conn() is not original_connection
    with patch("time.time", return_value=THROTTLE_CLOCK_START + THROTTLE_REOPEN_OFFSET_S):
        assert lane_queue.purge_due(min_interval_s=THROTTLE_INTERVAL_S) is False
    lane_queue._reset_queue_conn()
    with patch("time.time", return_value=THROTTLE_CLOCK_START + THROTTLE_INTERVAL_S):
        assert lane_queue.purge_due(min_interval_s=THROTTLE_INTERVAL_S) is True


def test_purge_throttle_store_read_failure_is_due():
    with patch.object(lane_queue, "_queue_op", side_effect=OSError("injected metadata read failure")):
        assert lane_queue.purge_due(min_interval_s=THROTTLE_INTERVAL_S) is True


def test_purge_throttle_malformed_stamp_is_due():
    with patch.object(lane_queue, "_queue_meta_get", return_value="invalid timestamp"):
        assert lane_queue.purge_due(min_interval_s=THROTTLE_INTERVAL_S) is True


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
