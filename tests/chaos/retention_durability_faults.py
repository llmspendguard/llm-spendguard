"""Power-loss model against real retention: only fsynced archive bytes survive."""
import atexit
import contextlib
import datetime
import json
import os
import stat
import tempfile
from unittest.mock import patch

isolated_home_owner = tempfile.TemporaryDirectory(prefix="sg-retention-chaos-")
atexit.register(isolated_home_owner.cleanup)
os.environ.setdefault("SPENDGUARD_HOME", isolated_home_owner.name)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

import spendguard  # noqa: E402
from spendguard import config, lane_queue  # noqa: E402

spendguard.require()

RETENTION_CHAOS_INTENT = "chaos:retention-durability"
RETENTION_CHAOS_TASK = "terminal history must survive power loss"
RETENTION_CHAOS_AGE_DAYS = 1
RETENTION_CHAOS_RETAIN_DAYS = 0


class RetentionPowerLoss(BaseException):
    """A process failure that bypasses ordinary purge error handling."""


def run_retention_durability_chaos_contract():
    mutation = os.getenv("SPENDGUARD_CHAOS_MUTATION", "intact")
    row_id = lane_queue.enqueue(RETENTION_CHAOS_INTENT, RETENTION_CHAOS_TASK)
    leased = lane_queue.lease(1)
    assert leased and leased[0]["id"] == row_id
    lane_queue.settle(row_id, {"text": RETENTION_CHAOS_TASK, "billed": False})
    old_timestamp = lane_queue._iso(lane_queue._utcnow() - datetime.timedelta(days=RETENTION_CHAOS_AGE_DAYS))
    with lane_queue._queue_op() as connection:
        connection.execute("UPDATE lane_queue SET updated_ts=? WHERE id=?", (old_timestamp, row_id))
    archive_path = config.HOME / "retention-chaos-archive.jsonl"
    stable_bytes = b""
    active_connections = []
    delete_committed = False
    real_queue_op = lane_queue._queue_op
    real_fsync = os.fsync
    real_append = lane_queue._append_durable_queue_archive

    def retention_track_archive_fsync(fd):
        nonlocal stable_bytes
        is_archive = (archive_path.exists() and
                      os.fstat(fd).st_ino == archive_path.stat().st_ino and
                      stat.S_ISREG(os.fstat(fd).st_mode))
        if is_archive and mutation == "skip_archive_fsync":
            return
        real_fsync(fd)
        if is_archive:
            stable_bytes = archive_path.read_bytes()

    class RetentionCrashConnection:
        def __init__(self, connection):
            self.connection = connection
            self.deleted = False

        def execute(self, statement, *args, **kwargs):
            nonlocal delete_committed
            result = self.connection.execute(statement, *args, **kwargs)
            if statement == "COMMIT" and self.deleted:
                delete_committed = True
                raise RetentionPowerLoss()
            return result

        def executemany(self, statement, *args, **kwargs):
            result = self.connection.executemany(statement, *args, **kwargs)
            if statement == "DELETE FROM lane_queue WHERE id=?":
                self.deleted = True
            return result

        def __getattr__(self, attribute):
            return getattr(self.connection, attribute)

    @contextlib.contextmanager
    def retention_crash_queue_op():
        with real_queue_op() as connection:
            wrapped = RetentionCrashConnection(connection)
            active_connections.append(wrapped)
            try:
                yield wrapped
            finally:
                active_connections.pop()

    def retention_reordered_append(path, records):
        if mutation == "delete_before_archive_durable":
            connection = active_connections[-1]
            connection.executemany("DELETE FROM lane_queue WHERE id=?", [(row_id,)])
            connection.execute("COMMIT")
        return real_append(path, records)

    with patch.object(lane_queue, "_queue_op", retention_crash_queue_op), \
            patch.object(os, "fsync", retention_track_archive_fsync), \
            patch.object(lane_queue, "_append_durable_queue_archive", retention_reordered_append):
        try:
            lane_queue.purge(retain_days=RETENTION_CHAOS_RETAIN_DAYS, archive_path=str(archive_path))
        except RetentionPowerLoss:
            pass
        else:
            raise AssertionError("power loss did not reach the DELETE commit boundary")

    # Discard volatile page-cache bytes; SQLite's committed state remains durable.
    archive_path.write_bytes(stable_bytes)
    with real_queue_op() as connection:
        live = connection.execute("SELECT id FROM lane_queue WHERE id=?", (row_id,)).fetchone()
    durable_records = [json.loads(line) for line in stable_bytes.splitlines()]
    archived = [record for record in durable_records if record["id"] == row_id]
    assert delete_committed, "fault must test a committed deletion, not a harmless rollback"
    assert live or archived, "terminal row deleted without a durable archive copy"
    assert archived and archived[0]["task"] == RETENTION_CHAOS_TASK
    print("GREEN retention durability: committed deletion survives archive page-cache loss")


if __name__ == "__main__":
    run_retention_durability_chaos_contract()
