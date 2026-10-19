"""Retention must preserve live history on either durability barrier failure."""
import datetime
import builtins
import contextlib
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix="sg-retention-barrier-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import config, lane_queue

RETENTION_FAILURE_AGE_DAYS = 2
RETENTION_FAILURE_KEEP_DAYS = 1
RETENTION_FAILURE_TASK = "retain this complete terminal history"


@pytest.mark.parametrize("failed_barrier", ("file", "directory", "readback"))
def test_retention_barrier_failure_keeps_terminal_rows(tmp_path, monkeypatch, failed_barrier):
    database = tmp_path / "queue.sqlite"
    archive = tmp_path / "archive.jsonl"
    monkeypatch.setattr(config, "lane_queue_db_path", lambda: str(database))
    old_timestamp = lane_queue._iso(lane_queue._utcnow() - datetime.timedelta(days=RETENTION_FAILURE_AGE_DAYS))
    with lane_queue._queue_op() as connection:
        cursor = connection.execute(
            "INSERT INTO lane_queue(intent,task,state,created_ts,updated_ts) VALUES(?,?,?,?,?)",
            ("retention:barrier-failure", RETENTION_FAILURE_TASK, "done", old_timestamp, old_timestamp))
        row_id = cursor.lastrowid
    real_fsync = os.fsync
    real_open = builtins.open
    visited_barriers = []

    @contextlib.contextmanager
    def retention_corrupt_readback(*args, **kwargs):
        with real_open(*args, **kwargs) as handle:
            if failed_barrier == "readback":
                visited_barriers.append("readback")
                with patch.object(handle, "read", return_value="injected corrupt archive readback"):
                    yield handle
            else:
                yield handle

    def retention_fail_selected_fsync(fd):
        barrier = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        visited_barriers.append(barrier)
        if barrier == failed_barrier:
            raise OSError("injected retention durability barrier failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", retention_fail_selected_fsync)
    monkeypatch.setattr(lane_queue, "open", retention_corrupt_readback, raising=False)
    result = lane_queue.purge(retain_days=RETENTION_FAILURE_KEEP_DAYS, archive_path=str(archive))
    assert result.get("error"), result
    assert result["deleted"] == 0
    assert failed_barrier in visited_barriers
    with lane_queue._queue_op() as connection:
        surviving = connection.execute("SELECT task FROM lane_queue WHERE id=?", (row_id,)).fetchone()
    assert surviving and surviving[0] == RETENTION_FAILURE_TASK


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
