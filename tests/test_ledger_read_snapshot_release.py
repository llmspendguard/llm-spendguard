"""Deadline recovery releases a measured WAL pin, including a close-loss mutation."""
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_read

SNAPSHOT_RELEASE_BUDGET_S = 0.05
SNAPSHOT_RELEASE_RECURSIVE_STEPS = 1000000000
SNAPSHOT_RELEASE_SCHEMA = "CREATE TABLE deadline_snapshot_probe (value)"
SNAPSHOT_RELEASE_INSERT = "INSERT INTO deadline_snapshot_probe VALUES (?)"
SNAPSHOT_RELEASE_SELECT = "SELECT * FROM deadline_snapshot_probe"
SNAPSHOT_RELEASE_SLOW_SQL = """WITH RECURSIVE slow(value) AS
    (VALUES(0) UNION ALL SELECT value+1 FROM slow WHERE value<?)
    SELECT SUM(value) FROM slow"""
SNAPSHOT_RELEASE_CHECKPOINT_SQL = "PRAGMA wal_checkpoint(TRUNCATE)"
SNAPSHOT_RELEASE_WAL_MODE_SQL = "PRAGMA journal_mode=WAL"
SNAPSHOT_RELEASE_AUTOCHECKPOINT_SQL = "PRAGMA wal_autocheckpoint=0"
SNAPSHOT_RELEASE_NO_WAIT_SQL = "PRAGMA busy_timeout=0"
SNAPSHOT_RELEASE_BEGIN_SQL = "BEGIN"


class LedgerDeadlineSnapshotReleaseGuards(unittest.TestCase):
    def test_deadline_disposes_read_snapshot_and_close_loss_mutation_pins_wal(self):
        """The mutated reader retains its actual snapshot; no timing proxy is used."""
        for suppress_recovery in (False, True):
            with self.subTest(suppress_recovery=suppress_recovery):
                ledger_path = pathlib.Path(tempfile.mkdtemp(dir=config.HOME)) / (
                    pathlib.Path(__file__).stem + str(suppress_recovery))
                writer = sqlite3.connect(ledger_path)
                writer.execute(SNAPSHOT_RELEASE_WAL_MODE_SQL)
                writer.execute(SNAPSHOT_RELEASE_AUTOCHECKPOINT_SQL)
                writer.execute(SNAPSHOT_RELEASE_NO_WAIT_SQL)
                writer.execute(SNAPSHOT_RELEASE_SCHEMA)
                writer.execute(SNAPSHOT_RELEASE_INSERT, (__name__,))
                writer.commit()
                original_open = config.open_ledger_connection
                retained_handles = []

                class SnapshotCloseLossMutation:
                    def __init__(self, raw, retained=retained_handles):
                        self.raw = raw
                        self.retained = retained

                    def __getattr__(self, name):
                        return getattr(self.raw, name)

                    def close(self):
                        # Deliberately reintroduce missing recovery for this owned reader.
                        self.retained.append(self.raw)

                    def __setattr__(self, name, value):
                        if name in ("raw", "retained"):
                            object.__setattr__(self, name, value)
                        else:
                            setattr(self.raw, name, value)

                    def rollback(self):
                        # Remove the entire transaction recovery, including rollback.
                        return None

                def open_snapshot_close_loss_mutant(*args, opener=original_open,
                                                    proxy=SnapshotCloseLossMutation, **kwargs):
                    return proxy(opener(*args, **kwargs))

                try:
                    with patch.object(config, "open_ledger_connection",
                                      side_effect=(open_snapshot_close_loss_mutant
                                                   if suppress_recovery else original_open)):
                        with self.assertRaises(ledger_read.LedgerReadTimeout):
                            with ledger_read._ledger_read_connection(
                                    ledger_path, SNAPSHOT_RELEASE_BUDGET_S) as reader:
                                reader.execute(SNAPSHOT_RELEASE_BEGIN_SQL)
                                reader.execute(SNAPSHOT_RELEASE_SELECT).fetchall()
                                writer.execute(SNAPSHOT_RELEASE_INSERT, (SNAPSHOT_RELEASE_SCHEMA,))
                                writer.commit()
                                self.assertNotEqual(
                                    writer.execute(SNAPSHOT_RELEASE_CHECKPOINT_SQL).fetchone()[0], 0,
                                    "fault injection must establish an actual pinned snapshot")
                                reader.execute(SNAPSHOT_RELEASE_SLOW_SQL,
                                               (SNAPSHOT_RELEASE_RECURSIVE_STEPS,)).fetchall()
                    checkpoint = writer.execute(SNAPSHOT_RELEASE_CHECKPOINT_SQL).fetchone()
                    if suppress_recovery:
                        # Execute precisely the normal postcondition under the
                        # recovery-loss mutant: that guard must FAIL.
                        with self.assertRaises(AssertionError):
                            self.assertEqual(checkpoint, (0, 0, 0),
                                             "deadline must immediately release the WAL snapshot")
                    else:
                        self.assertEqual(checkpoint, (0, 0, 0),
                                         "deadline must immediately release the WAL snapshot")
                finally:
                    for retained_handle in retained_handles:
                        retained_handle.close()
                    writer.close()


if __name__ == "__main__":
    unittest.main()
