"""Offline guards for auxiliary mutation admission before subsystem locks."""
import os
import contextlib
import subprocess
import sys
import tempfile
from unittest.mock import patch


def test_auxiliary_mutations_enter_writer_before_lock():
    from spendguard import budget, tag
    from spendguard.ledger_writer import current_ledger_writer_connection

    class AuxiliaryWriterObservedLock:
        def __enter__(self):
            assert current_ledger_writer_connection() is not None, "subsystem lock wraps writer dispatch"

        def __exit__(self, *details):
            pass

    class AuxiliaryTagLedgerFixture:
        def bulk_retag_project(self, *args, **kwargs):
            return 0

    with patch.object(budget, "_lock", AuxiliaryWriterObservedLock()), \
            patch.object(budget, "_ledger", return_value=AuxiliaryTagLedgerFixture()), \
            patch.object(tag, "backfill_project_from_venv", return_value=0):
        assert tag.retag_deterministic() == 0
        assert tag.move_project(__name__, __name__) == 0


def test_content_schema_admission_precedes_retention_lock():
    from spendguard import content_retention
    retention_lock_held = []

    class ObservedRetentionLock:
        def __enter__(self):
            retention_lock_held.append(True)

        def __exit__(self, *details):
            retention_lock_held.pop()

    class RetentionSchemaProbeConnection:
        def execute(self, *args):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    def admit_retention_schema_probe():
        assert not retention_lock_held, "retention lock wraps schema writer admission"
        return RetentionSchemaProbeConnection()

    with patch.object(content_retention, "content_retention_enabled", return_value=True), \
            patch.object(content_retention, "_content_retention_lock", ObservedRetentionLock()), \
            patch.object(content_retention, "_content_retention_db", admit_retention_schema_probe):
        assert content_retention.record_governed_call_content(__name__, {}, {}, {}) is not None


def test_deadletter_writer_admission_precedes_spool_lock():
    from spendguard import ledger_deadletter
    from spendguard.ledger_writer import current_ledger_writer_connection

    @contextlib.contextmanager
    def observe_writer_before_spool_lock(candidate):
        assert current_ledger_writer_connection() is not None, "spool lock wraps writer admission"
        yield candidate

    with tempfile.NamedTemporaryFile() as empty_deadletter:
        from pathlib import Path
        with patch.object(ledger_deadletter, "locked_spend_deadletter_path", observe_writer_before_spool_lock):
            ledger_deadletter.drain_spend_deadletter_events([Path(empty_deadletter.name)])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_auxiliary_mutations_enter_writer_before_lock()
        test_content_schema_admission_precedes_retention_lock()
        test_deadletter_writer_admission_precedes_spool_lock()
    else:
        with tempfile.TemporaryDirectory() as auxiliary_isolated_home:
            subprocess.run(
                [sys.executable, __file__, "--auxiliary-lock-probe"],
                env={**os.environ, "SPENDGUARD_HOME": auxiliary_isolated_home,
                     "SPENDGUARD_TEST_ISOLATED": "1"}, check=True,
            )
