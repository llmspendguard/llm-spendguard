"""Force the archive CI failure: an observer polls inside rmtree's deletion boundary."""
import contextlib
import os
from pathlib import Path
import shutil
import tempfile
import threading

import pytest

from spendguard import execution_journal
from test_adversarial_archive_evidence import test_remote_codex_broker_keeps_partial_transport_evidence
from test_durable_delegate_evidence import install_controlled_durable_observer_spawn

OBSERVER_RECLAMATION_WAIT_S = execution_journal.EXECUTION_OBSERVER_START_BOUND_DEFAULT_S


def test_execution_observer_cannot_resurrect_reclaimed_archive():
    with tempfile.TemporaryDirectory() as archive_root, pytest.MonkeyPatch.context() as patch:
        # The original broker/drop/recovery assertions run unchanged. Control only
        # observer scheduling, rather than depending on detached process latency.
        install_controlled_durable_observer_spawn(patch)
        test_remote_codex_broker_keeps_partial_transport_evidence(Path(archive_root), patch)
        execution_directory, = Path(archive_root).iterdir()
        execution_id = execution_directory.name
        original_lock = execution_journal._locked_durable_execution
        original_rmdir = os.rmdir
        observer_at_lock = threading.Event()
        reclaim_snapshot_removed = threading.Event()
        observer_finished = threading.Event()
        observer_errors = []

        @contextlib.contextmanager
        def acquire_archive_lock_after_cleanup_snapshot(*args, **kwargs):
            observer_at_lock.set()
            assert reclaim_snapshot_removed.wait(OBSERVER_RECLAMATION_WAIT_S)
            with original_lock(*args, **kwargs) as locked_directory:
                yield locked_directory

        def poll_archive_observer_at_cleanup_boundary():
            try:
                execution_journal._observe_durable_execution(
                    execution_id, archive_root, execution_journal.EXECUTION_WATCH_INTERVAL_DEFAULT_S)
            except BaseException as observer_error:
                observer_errors.append(observer_error)
            finally:
                observer_finished.set()

        def remove_archive_directory_after_observer_poll(path, *args, **kwargs):
            if Path(path).name == execution_id:
                # rmtree has enumerated and unlinked every file. The real observer
                # now polls before rmdir, exactly the ordering behind CI ENOTEMPTY.
                reclaim_snapshot_removed.set()
                assert observer_finished.wait(OBSERVER_RECLAMATION_WAIT_S)
            return original_rmdir(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as ordering_patch:
            ordering_patch.setattr(execution_journal, "_locked_durable_execution",
                                   acquire_archive_lock_after_cleanup_snapshot)
            ordering_patch.setattr(os, "rmdir", remove_archive_directory_after_observer_poll)
            observer = threading.Thread(target=poll_archive_observer_at_cleanup_boundary)
            observer.start()
            try:
                assert observer_at_lock.wait(OBSERVER_RECLAMATION_WAIT_S)
                shutil.rmtree(execution_directory)
                assert not execution_directory.exists(), "observer resurrected a reclaimed execution"
                assert not observer_errors, observer_errors
            finally:
                reclaim_snapshot_removed.set()
                observer.join(OBSERVER_RECLAMATION_WAIT_S)
                assert not observer.is_alive()


if __name__ == "__main__":
    test_execution_observer_cannot_resurrect_reclaimed_archive()
    print("OK: observer cannot resurrect reclaimed archive; exact partial transport evidence preserved")
