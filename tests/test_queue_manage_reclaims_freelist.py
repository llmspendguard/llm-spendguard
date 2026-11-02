"""Real SQLite legacy-file regression; never uses an operator database."""
import sqlite3
import tempfile
import datetime
from pathlib import Path

import pytest

from spendguard import config, lane_queue
from spendguard.capability_readiness_gates import assess_lane_queue_maintenance_readiness

RECLAIM_TEST_ROWS = 256
RECLAIM_TEST_PAYLOAD_BYTES = 8192


def test_queue_manage_reclaims_legacy_freelist(tmp_path, monkeypatch):
    database = tmp_path / "legacy-queue.db"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "lane_queue_db_path", lambda: str(database))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_padding(payload BLOB)")
        lane_queue._ensure_queue_schema(connection)
        connection.executemany("INSERT INTO legacy_padding VALUES(zeroblob(?))",
                               [(RECLAIM_TEST_PAYLOAD_BYTES,)] * RECLAIM_TEST_ROWS)
        connection.execute("DELETE FROM legacy_padding")
        now = lane_queue._iso(lane_queue._utcnow())
        connection.execute("INSERT INTO lane_queue(intent,state,created_ts,updated_ts) VALUES(?,?,?,?)",
                           ("freelist-regression", "done", now, now))
        old = lane_queue._iso(lane_queue._utcnow() - datetime.timedelta(
            days=lane_queue.RETAIN_DAYS_DEFAULT * 2))
        connection.execute("INSERT INTO lane_queue(intent,state,created_ts,updated_ts) VALUES(?,?,?,?)",
                           ("freelist-regression", "done", old, old))
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        before = connection.execute("PRAGMA freelist_count").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        assert before / pages > lane_queue.QUEUE_FREELIST_RATIO_LIMIT_DEFAULT
    size_before = database.stat().st_size
    report = lane_queue.manage_queue_tick(execute=True)
    assert "error" not in report, report
    assert report["purge"]["deleted"] == 1
    with sqlite3.connect(database) as connection:
        after = connection.execute("PRAGMA freelist_count").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        assert after / pages < lane_queue.QUEUE_FREELIST_RATIO_LIMIT_DEFAULT
        assert connection.execute("SELECT COUNT(*) FROM lane_queue").fetchone()[0] == 1
    assert database.stat().st_size < size_before
    readiness = assess_lane_queue_maintenance_readiness(context={}, database_path=database)
    assert readiness.status == "PASS", readiness
    assert "old_terminal=0/0" in readiness.evidence[0]
    print(f"legacy tick: freelist={before}->{after}; bytes={size_before}->{database.stat().st_size}; "
          f"retained_rows=1; old_terminal=0; readiness={readiness.status}")
    # A real independent SQLite writer must prevent maintenance from vacuuming.
    with sqlite3.connect(database) as writer:
        writer.executemany("INSERT INTO legacy_padding VALUES(zeroblob(?))",
                           [(RECLAIM_TEST_PAYLOAD_BYTES,)] * RECLAIM_TEST_ROWS)
        writer.execute("DELETE FROM legacy_padding")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            lane_queue._reclaim_queue_freelist()
        writer.rollback()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM lane_queue").fetchone()[0] == 1
    original_qcfg = lane_queue._qcfg
    monkeypatch.setattr(lane_queue, "_qcfg", lambda name, default:
                        0.0 if name == "queue_reclaim_timeout_s" else original_qcfg(name, default))
    monkeypatch.setattr(lane_queue, "QUEUE_RECLAIM_PROGRESS_OPS", 1)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        lane_queue._reclaim_queue_freelist()
    monkeypatch.setattr(lane_queue, "_qcfg", original_qcfg)
    # An interrupted VACUUM is atomic; the following tick can retry safely.
    assert "error" not in lane_queue.manage_queue_tick(execute=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM lane_queue").fetchone()[0] == 1


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as reclaim_home, pytest.MonkeyPatch.context() as reclaim_patch:
        test_queue_manage_reclaims_legacy_freelist(Path(reclaim_home), reclaim_patch)
