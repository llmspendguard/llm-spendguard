"""Regression contracts for bounded delegate-worktree retention."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from spendguard import config, delegate_jobs, delegate_router, delegate_worktrees, execution_journal


RETENTION_TEST_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RETENTION_TEST_FAILURE = "injected delegate failure"
RETENTION_TEST_REPOSITORY_NAME = "repository"
RETENTION_TEST_WORKTREE_NAME = "workspace"
RETENTION_TEST_JOURNAL_NAME = "journal"
RETENTION_TEST_EXECUTION_NAME = "execution"
RETENTION_TEST_PRIMARY_NAME = "primary"
RETENTION_TEST_FUTURE_WINDOW_S = 120
RETENTION_TEST_ADVANCE_S = 121
RETENTION_TEST_ORPHAN_GRACE_S = 240
RETENTION_TEST_OLD_AGE_S = 241
RETENTION_TEST_YOUNG_AGE_S = 239
RETENTION_TEST_CONFIGURED_WINDOW_S = 360
RETENTION_TEST_JOB_ID = "retention-job"
RETENTION_TEST_EXECUTION_ID = "retention-execution"
RETENTION_TEST_DEADLINE_FIELD = "2030-01-01T00:00:00+00:00"
RETENTION_TEST_REAPER_FAILURE = "injected worktree sweep failure"
RETENTION_TEST_TASK_FINGERPRINT = "retention-task-fingerprint"
RETENTION_TEST_CWD = "/retention-test-cwd"
RETENTION_TEST_PROVIDER = "retention-test-provider"
RETENTION_TEST_INTENT = "retention-test-intent"
RETENTION_TEST_SESSION_REAP_RESULT = {"reaped": [RETENTION_TEST_JOB_ID], "errors": []}


def _raise_retention_reaper_failure():
    raise RuntimeError(RETENTION_TEST_REAPER_FAILURE)


def test_opportunistic_sweep_failure_is_observable_without_breaking_job_creation():
    connection = MagicMock()
    connection_context = MagicMock()
    connection_context.__enter__.return_value = connection
    with patch.object(delegate_jobs, "prune_delegate_jobs"), patch.object(
            delegate_worktrees, "reclaim_expired_delegate_worktrees",
            side_effect=_raise_retention_reaper_failure), patch.object(
            config, "warn_once") as warning, patch.object(
            execution_journal, "begin_durable_execution", return_value=RETENTION_TEST_EXECUTION_ID), patch.object(
            delegate_jobs, "_open_delegate_jobs_connection", return_value=connection_context):
        job_id = delegate_jobs.create_delegate_job(
            kind=delegate_jobs.DELEGATE_JOB_RUNNING_STATE,
            task_fingerprint=RETENTION_TEST_TASK_FINGERPRINT,
            cwd=RETENTION_TEST_CWD,
            provider=RETENTION_TEST_PROVIDER,
            intent=RETENTION_TEST_INTENT,
            dirty_before=set())
    assert job_id
    connection.execute.assert_called_once()
    warning.assert_called_once()
    assert RETENTION_TEST_REAPER_FAILURE in warning.call_args.args[0]


def test_scheduled_sweep_failure_is_observable_and_session_reaping_still_runs():
    with patch.object(delegate_worktrees, "reclaim_expired_delegate_worktrees",
                      side_effect=_raise_retention_reaper_failure), patch.object(
            delegate_jobs, "reap_terminal_codex_sessions",
            return_value=RETENTION_TEST_SESSION_REAP_RESULT) as session_reaper, patch.object(
            config, "warn_once") as warning:
        result = execution_journal.reap_execution_codex_sessions()
    assert result == RETENTION_TEST_SESSION_REAP_RESULT
    session_reaper.assert_called_once_with()
    warning.assert_called_once()
    assert RETENTION_TEST_REAPER_FAILURE in warning.call_args.args[0]


def test_failed_isolated_delegate_stamps_reclaim_deadline():
    persisted = []
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / RETENTION_TEST_REPOSITORY_NAME
        repository.mkdir()
        worktree = Path(temporary) / RETENTION_TEST_WORKTREE_NAME

        def fake_delegate_worktree_git(cwd, *arguments, **kwargs):
            if arguments == ("rev-parse", "--show-toplevel"):
                return f"{repository.resolve()}\n".encode()
            if arguments[:2] == ("worktree", "add"):
                worktree.mkdir()
                return b""
            if arguments == ("diff", "--binary", "HEAD", "--"):
                return b""
            if arguments == ("ls-files", "--others", "--exclude-standard", "-z"):
                return b""
            if arguments == ("add", "-A"):
                return b""
            if arguments == ("write-tree",):
                return b"baseline\n"
            raise AssertionError(f"unexpected git operation: {arguments}")

        def fail_delegate(_cwd):
            raise RuntimeError(RETENTION_TEST_FAILURE)

        def create_fixture_worktree(**_kwargs):
            worktree.mkdir()
            return str(worktree)

        with patch.object(delegate_worktrees, "_delegate_worktree_git", fake_delegate_worktree_git), patch.object(
                delegate_worktrees.tempfile, "mkdtemp", side_effect=create_fixture_worktree):
            try:
                delegate_worktrees.run_isolated_delegate(
                    fail_delegate, repository, worktree_root=temporary,
                    metadata_writer=lambda metadata: persisted.append(dict(metadata)),
                    now=RETENTION_TEST_NOW, retention_s=RETENTION_TEST_FUTURE_WINDOW_S)
            except RuntimeError as error:
                assert str(error) == RETENTION_TEST_FAILURE
            else:
                raise AssertionError("injected delegate failure was swallowed")

    expected = RETENTION_TEST_NOW.timestamp() + RETENTION_TEST_FUTURE_WINDOW_S
    assert datetime.fromisoformat(persisted[-1]["reclaim_not_before"]).timestamp() == expected


def _write_retention_sidecar(journal_root, metadata):
    sidecar = journal_root / RETENTION_TEST_EXECUTION_NAME / delegate_worktrees.DELEGATE_WORKTREE_METADATA_FILENAME
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")
    return sidecar


def _fake_reclaiming_git(calls):
    def reclaim(primary_cwd, *arguments, **_kwargs):
        calls.append((Path(primary_cwd), arguments))
        worktree = Path(arguments[-1])
        gitfile = worktree / ".git"
        if gitfile.exists():
            gitfile.unlink()
        worktree.rmdir()
        return b""
    return reclaim


def test_deadline_floor_then_same_tree_is_reclaimed():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / delegate_worktrees.DELEGATE_WORKTREE_DIRECTORY
        root.mkdir()
        tree = root / RETENTION_TEST_WORKTREE_NAME
        tree.mkdir()
        primary = base / RETENTION_TEST_PRIMARY_NAME
        primary.mkdir()
        journal = base / RETENTION_TEST_JOURNAL_NAME
        deadline = RETENTION_TEST_NOW.timestamp() + RETENTION_TEST_FUTURE_WINDOW_S
        sidecar = _write_retention_sidecar(journal, {
            "path": str(tree), "primary_cwd": str(primary), "retained": True,
            "reclaim_not_before": datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec="seconds")})
        calls = []
        with patch("spendguard.execution_journal.durable_execution_store_root", return_value=journal) as resolver, patch.object(
                delegate_worktrees, "_delegate_worktree_git", _fake_reclaiming_git(calls)):
            inside = delegate_worktrees.reclaim_expired_delegate_worktrees(
                RETENTION_TEST_NOW, journal_root_resolver=resolver, metadata_updater=config.update_json,
                worktree_root=root,
                orphan_grace_s=RETENTION_TEST_ORPHAN_GRACE_S)
            assert inside == {"reclaimed": 0, "retained": 1, "gaps": []}
            assert tree.exists() and not calls
            after = delegate_worktrees.reclaim_expired_delegate_worktrees(
                datetime.fromtimestamp(RETENTION_TEST_NOW.timestamp() + RETENTION_TEST_ADVANCE_S, timezone.utc),
                journal_root_resolver=resolver, metadata_updater=config.update_json, worktree_root=root,
                orphan_grace_s=RETENTION_TEST_ORPHAN_GRACE_S)
        assert after == {"reclaimed": 1, "retained": 0, "gaps": []}
        assert not tree.exists() and json.loads(sidecar.read_text())["retained"] is False


def test_orphan_age_and_unresolved_gap_are_visible():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / delegate_worktrees.DELEGATE_WORKTREE_DIRECTORY
        root.mkdir()
        primary = base / RETENTION_TEST_PRIMARY_NAME
        registered = primary / ".git" / "worktrees"
        registered.mkdir(parents=True)
        old_tree = root / "old"
        young_tree = root / "young"
        unresolved_tree = root / "unresolved"
        for tree in (old_tree, young_tree, unresolved_tree):
            tree.mkdir()
        for tree in (old_tree, young_tree):
            (tree / ".git").write_text(
                f"{delegate_worktrees.DELEGATE_WORKTREE_GITDIR_PREFIX}{registered / tree.name}\n")
        old_timestamp = RETENTION_TEST_NOW.timestamp() - RETENTION_TEST_OLD_AGE_S
        young_timestamp = RETENTION_TEST_NOW.timestamp() - RETENTION_TEST_YOUNG_AGE_S
        for tree in (old_tree, unresolved_tree):
            os.utime(tree, (old_timestamp, old_timestamp))
        os.utime(young_tree, (young_timestamp, young_timestamp))
        journal = base / RETENTION_TEST_JOURNAL_NAME
        calls = []
        with patch("spendguard.execution_journal.durable_execution_store_root", return_value=journal) as resolver, patch.object(
                delegate_worktrees, "_delegate_worktree_git", _fake_reclaiming_git(calls)):
            report = delegate_worktrees.reclaim_expired_delegate_worktrees(
                RETENTION_TEST_NOW, journal_root_resolver=resolver, metadata_updater=config.update_json,
                worktree_root=root,
                orphan_grace_s=RETENTION_TEST_ORPHAN_GRACE_S)
        assert report["reclaimed"] == 1 and report["retained"] == 1
        assert len(report["gaps"]) == 1 and report["gaps"][0]["path"] == str(unresolved_tree)
        assert not old_tree.exists() and young_tree.exists() and unresolved_tree.exists()


def test_retention_window_is_config_driven():
    original_get = config._cfg_get
    def configured_get(section, key, default=None):
        if section == "delegate" and key == "worktree_retention_s":
            return RETENTION_TEST_CONFIGURED_WINDOW_S
        return original_get(section, key, default)
    with patch.object(config, "_cfg_get", configured_get):
        execution = {"worktree": {"retained": True}}
        retention_s = delegate_router._configured_delegate_worktree_retention_seconds(
            delegate_worktrees.DELEGATE_WORKTREE_DEFAULT_RETENTION_S)
        delegate_worktrees.schedule_delegate_worktree_reclaim(
            execution, now=RETENTION_TEST_NOW, retention_s=retention_s)
        stamped = datetime.fromisoformat(execution["worktree"]["reclaim_not_before"])
        assert stamped.timestamp() - RETENTION_TEST_NOW.timestamp() == RETENTION_TEST_CONFIGURED_WINDOW_S


def test_both_job_channels_project_reclaim_deadline():
    row = {"job_id": RETENTION_TEST_JOB_ID, "execution_id": RETENTION_TEST_EXECUTION_ID,
           "state": delegate_jobs.DELEGATE_JOB_DONE_STATE, "result_payload": json.dumps({"status": "executed"}),
           "dirty_before": "[]", "terminal_reason": None, "error": None}
    details = {"worktree": {"reclaim_not_before": RETENTION_TEST_DEADLINE_FIELD}}
    with patch.object(delegate_jobs, "_delegate_job_row", return_value=row), patch.object(
            delegate_jobs, "_delegate_job_workspace_details", return_value=details):
        assert delegate_jobs.read_delegate_job(RETENTION_TEST_JOB_ID)["worktree"]["reclaim_not_before"] == RETENTION_TEST_DEADLINE_FIELD
        assert delegate_jobs.read_delegate_job_result(RETENTION_TEST_JOB_ID)["worktree"]["reclaim_not_before"] == RETENTION_TEST_DEADLINE_FIELD


if __name__ == "__main__":
    test_opportunistic_sweep_failure_is_observable_without_breaking_job_creation()
    test_scheduled_sweep_failure_is_observable_and_session_reaping_still_runs()
    test_failed_isolated_delegate_stamps_reclaim_deadline()
    test_deadline_floor_then_same_tree_is_reclaimed()
    test_orphan_age_and_unresolved_gap_are_visible()
    test_retention_window_is_config_driven()
    test_both_job_channels_project_reclaim_deadline()
    print("delegate worktree retention contracts: PASS")
