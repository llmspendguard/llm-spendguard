"""Guard the independent storage and hold budgets in free-lane recovery."""
import atexit
import os
import pathlib
import sys
import tempfile
import time

isolated_home_owner = tempfile.TemporaryDirectory(prefix="sg-hold-admission-")
atexit.register(isolated_home_owner.cleanup)
os.environ["SPENDGUARD_HOME"] = isolated_home_owner.name
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import lane_queue  # noqa: E402


HOLD_ADMISSION_GUARD_DEADLINE_S = 0.02
HOLD_ADMISSION_GUARD_DELAY_FACTOR = 2
HOLD_ADMISSION_GUARD_POLL_S = 0.001


def test_tiny_hold_deadline_does_not_starve_durable_admission():
    original_record_open = lane_queue.record_open

    def record_open_after_storage_delay(intent, task, *, sla_s):
        time.sleep(HOLD_ADMISSION_GUARD_DEADLINE_S * HOLD_ADMISSION_GUARD_DELAY_FACTOR)
        return original_record_open(intent, task, sla_s=sla_s)

    lane_queue.record_open = record_open_after_storage_delay
    try:
        result = lane_queue.hold_free_recovery(
            __name__, __name__, lambda: {"text": "recovered"},
            deadline_s=HOLD_ADMISSION_GUARD_DEADLINE_S,
            poll_s=HOLD_ADMISSION_GUARD_POLL_S,
        )
    finally:
        lane_queue.record_open = original_record_open

    assert result["text"] == "recovered"
    assert result["_free_hold"]["deadline_exhausted"] is False


if __name__ == "__main__":
    test_tiny_hold_deadline_does_not_starve_durable_admission()
    print("[OK] tiny hold deadline preserves durable admission")
