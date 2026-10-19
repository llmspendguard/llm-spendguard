"""Bounded drains expose leased work and unavailable counts to programmatic callers."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix="sg-drain-cap-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import lane_balance, lane_queue

CAP_TEST_BATCH_SIZE = 1
CAP_TEST_MAX_ITERS = 1
CAP_TEST_IDLE_ROUNDS = 1
CAP_TEST_IDLE_SLEEP_S = 0.0
CAP_TEST_LOAD_CEILING = 0.0
CAP_TEST_INTENT = "guard:drain-cap"


def _run_honesty_guard_drain(max_iters):
    return lane_queue.drain(batch=CAP_TEST_BATCH_SIZE, idle_rounds=CAP_TEST_IDLE_ROUNDS,
                            idle_sleep=CAP_TEST_IDLE_SLEEP_S, load_ceiling=CAP_TEST_LOAD_CEILING,
                            max_iters=max_iters)


def test_drain_cap_reports_actual_leased_rows():
    row_id = lane_queue.enqueue(CAP_TEST_INTENT, "leave this work leased")
    assert row_id is not None
    with patch.object(lane_queue, "_qcfg", side_effect=lambda name, default: 0), \
            patch.object(lane_balance, "bulk_delegate", return_value=[]):
        summary = _run_honesty_guard_drain(CAP_TEST_MAX_ITERS)
    assert summary.get("capped") is True, summary
    assert summary["remaining_at_cap"]["leased"] > 0, summary
    assert summary["remaining_at_cap"]["pending"] == 0, summary
    # Finish the deliberately stranded fixture through the real state transition.
    assert lane_queue.settle(row_id, {"text": "fixture recovered", "billed": False})


def test_drain_cap_clean_completion_and_uncapped_idle():
    assert lane_queue.enqueue(CAP_TEST_INTENT, "finish this work") is not None
    with patch.object(lane_queue, "_qcfg", side_effect=lambda name, default: 0), \
            patch.object(lane_balance, "bulk_delegate", return_value=[{"text": "done", "billed": False}]):
        summary = _run_honesty_guard_drain(CAP_TEST_MAX_ITERS)
        uncapped = _run_honesty_guard_drain(None)
    assert summary["done"] == CAP_TEST_BATCH_SIZE, summary
    assert not summary.get("capped"), summary
    assert not uncapped.get("capped"), uncapped
    assert "remaining_at_cap" not in summary


def test_drain_cap_unknown_depth_is_explicit():
    with patch.object(lane_queue, "_qcfg", side_effect=lambda name, default: 0), \
            patch.object(lane_queue, "_overloaded", return_value=True), \
            patch.object(lane_queue, "queue_depth", return_value={}):
        summary = _run_honesty_guard_drain(CAP_TEST_MAX_ITERS)
    assert summary.get("capped") is True, summary
    assert summary["remaining_at_cap"]
    assert all(value is None for value in summary["remaining_at_cap"].values()), summary


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
