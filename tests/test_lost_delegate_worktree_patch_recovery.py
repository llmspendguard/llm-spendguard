"""Offline real-git contracts for retained lost-owner patch recovery."""
import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

LOST_PATCH_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
LOST_PATCH_SOURCE_NAME = "source.txt"
LOST_PATCH_BASE_TEXT = "committed\n"
LOST_PATCH_DIRTY_TEXT = "dirty input\n"
LOST_PATCH_OUTPUT_TEXT = "recovered output\n"
LOST_PATCH_AUTHOR = "Recovery fixture"
LOST_PATCH_EMAIL = "recovery@example.invalid"
LOST_PATCH_COMMIT_MESSAGE = "fixture baseline"
LOST_PATCH_FAILURE_MESSAGE = "simulated owner failure"
LOST_PATCH_JOB_ID = "recovery-fixture"
LOST_PATCH_EXECUTION_ID = "recovery-execution"
LOST_PATCH_ENVELOPE_NAME = "envelope.json"
sys.path.insert(0, os.fspath(LOST_PATCH_SOURCE_ROOT))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
from spendguard.manage.token_operations import delegate_worktrees, delegate_jobs, execution_journal


@contextlib.contextmanager
def lost_patch_fixture_workspace():
    with tempfile.TemporaryDirectory() as temporary:
        fixture_root = Path(temporary)
        repository = fixture_root / "repository"
        repository.mkdir()
        for arguments in (("init",), ("config", "user.name", LOST_PATCH_AUTHOR),
                          ("config", "user.email", LOST_PATCH_EMAIL)):
            delegate_worktrees._delegate_worktree_git(repository, *arguments)
        source = repository / LOST_PATCH_SOURCE_NAME
        source.write_text(LOST_PATCH_BASE_TEXT)
        delegate_worktrees._delegate_worktree_git(repository, "add", "-A")
        delegate_worktrees._delegate_worktree_git(repository, "-c", "core.hooksPath=" + os.devnull,
                                                  "commit", "-m", LOST_PATCH_COMMIT_MESSAGE)
        source.write_text(LOST_PATCH_DIRTY_TEXT)
        envelope = fixture_root / LOST_PATCH_ENVELOPE_NAME
        yield repository, fixture_root, envelope


def lost_patch_fixture_executor(workspace):
    (Path(workspace) / LOST_PATCH_SOURCE_NAME).write_text(LOST_PATCH_OUTPUT_TEXT)
    return {}


def lost_patch_fixture_capture(repository, fixture_root, envelope, executor):
    return delegate_worktrees.run_isolated_delegate(
        executor, repository, worktree_root=fixture_root / delegate_worktrees.DELEGATE_WORKTREE_DIRECTORY,
        metadata_writer=lambda metadata: delegate_worktrees.write_delegate_worktree_details(envelope, metadata))


def lost_patch_fixture_uncaptured(repository, fixture_root, envelope):
    def lost_patch_fixture_dead_executor(workspace):
        lost_patch_fixture_executor(workspace)
        raise RuntimeError(LOST_PATCH_FAILURE_MESSAGE)
    try:
        lost_patch_fixture_capture(repository, fixture_root, envelope, lost_patch_fixture_dead_executor)
    except RuntimeError as error:
        assert str(error) == LOST_PATCH_FAILURE_MESSAGE
    else:
        raise AssertionError("executor did not fail")
    metadata = delegate_worktrees.read_delegate_worktree_details(envelope)["worktree"]
    metadata.pop("reclaim_not_before", None)
    delegate_worktrees.write_delegate_worktree_details(envelope, metadata)
    return metadata


def check_lost_patch_success_capture():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        result = lost_patch_fixture_capture(repository, fixture_root, envelope, lost_patch_fixture_executor)
        metadata = result["worktree"]
        assert metadata["patch"] and metadata["baseline_tree"]
        assert metadata["changed_files"] == [LOST_PATCH_SOURCE_NAME]
        assert LOST_PATCH_DIRTY_TEXT.strip() in metadata["patch"]


def check_lost_patch_baseline_before_executor():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        def lost_patch_fixture_observe_baseline(workspace):
            persisted = delegate_worktrees.read_delegate_worktree_details(envelope)["worktree"]
            assert persisted["baseline_tree"]
            assert "patch" not in persisted
            raise RuntimeError(LOST_PATCH_FAILURE_MESSAGE)
        try:
            lost_patch_fixture_capture(repository, fixture_root, envelope, lost_patch_fixture_observe_baseline)
        except RuntimeError as error:
            assert str(error) == LOST_PATCH_FAILURE_MESSAGE
        else:
            raise AssertionError("executor did not fail")
        assert delegate_worktrees.read_delegate_worktree_details(envelope)["worktree"]["baseline_tree"]


def check_lost_patch_recovery_derives_capture():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        metadata = lost_patch_fixture_uncaptured(repository, fixture_root, envelope)
        # Committed output is recoverable as well as unstaged edits.
        workspace = Path(metadata["path"])
        delegate_worktrees._delegate_worktree_git(workspace, "add", "-A")
        delegate_worktrees._delegate_worktree_git(workspace, "-c", "core.hooksPath=" + os.devnull,
                                                  "commit", "-m", LOST_PATCH_COMMIT_MESSAGE)
        recovered = delegate_worktrees.recover_retained_delegate_worktree_patch(metadata)
        assert recovered is metadata and recovered["patch"]
        assert recovered["changed_files"] == [LOST_PATCH_SOURCE_NAME]
        assert recovered["reclaim_not_before"]


def check_lost_patch_missing_baseline_noop():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        metadata = lost_patch_fixture_uncaptured(repository, fixture_root, envelope)
        del metadata["baseline_tree"]
        previous = copy.deepcopy(metadata)
        assert delegate_worktrees.recover_retained_delegate_worktree_patch(metadata) is metadata
        assert metadata == previous and "patch" not in metadata


def check_lost_patch_existing_capture_noop():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        metadata = lost_patch_fixture_capture(repository, fixture_root, envelope,
                                             lost_patch_fixture_executor)["worktree"]
        previous = copy.deepcopy(metadata)
        (Path(metadata["path"]) / LOST_PATCH_SOURCE_NAME).write_text(LOST_PATCH_BASE_TEXT)
        assert delegate_worktrees.recover_retained_delegate_worktree_patch(metadata) is metadata
        assert metadata == previous


def check_lost_patch_job_recovery_surfaces_sidecar():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        lost_patch_fixture_uncaptured(repository, fixture_root, envelope)
        row = {"execution_id": LOST_PATCH_EXECUTION_ID, "state": delegate_jobs.DELEGATE_JOB_RUNNING_STATE,
               "result_payload": None, "terminal_reason": None, "error": None}
        def lost_patch_fixture_finish(job_id, *, state, **kwargs):
            assert job_id == LOST_PATCH_JOB_ID
            row["state"] = state
        with patch.object(delegate_jobs, "_delegate_job_row", return_value=row), \
                patch.object(delegate_jobs, "finish_delegate_job", side_effect=lost_patch_fixture_finish), \
                patch.object(execution_journal, "recover_durable_execution", return_value={"state": "lost"}), \
                patch.object(execution_journal, "durable_execution_payload_paths", return_value={"envelope": envelope}):
            assert delegate_jobs.recover_delegate_job_execution(LOST_PATCH_JOB_ID)["state"] == delegate_jobs.DELEGATE_JOB_LOST_STATE
            persisted = delegate_worktrees.read_delegate_worktree_details(envelope)["worktree"]
            assert persisted["patch"] and persisted["reclaim_not_before"]
            result = delegate_jobs.read_delegate_job_result(LOST_PATCH_JOB_ID)
            assert result["worktree"]["patch"] == persisted["patch"]
            assert result["worktree"]["changed_files"] == [LOST_PATCH_SOURCE_NAME]


def check_lost_patch_git_failure_preserves_metadata():
    with lost_patch_fixture_workspace() as (repository, fixture_root, envelope):
        metadata = lost_patch_fixture_uncaptured(repository, fixture_root, envelope)
        previous = copy.deepcopy(metadata)
        original_git = delegate_worktrees._delegate_worktree_git
        def lost_patch_fixture_fail_name_capture(cwd, *arguments, **kwargs):
            if "--name-only" in arguments:
                raise RuntimeError(LOST_PATCH_FAILURE_MESSAGE)
            return original_git(cwd, *arguments, **kwargs)
        diagnostics = io.StringIO()
        with patch.object(delegate_worktrees, "_delegate_worktree_git", side_effect=lost_patch_fixture_fail_name_capture), \
                contextlib.redirect_stderr(diagnostics):
            assert delegate_worktrees.recover_retained_delegate_worktree_patch(metadata) is metadata
        assert metadata == previous
        assert "recover_retained_delegate_worktree_patch" in diagnostics.getvalue()


if __name__ == "__main__":
    lost_patch_checks = (check_lost_patch_success_capture, check_lost_patch_baseline_before_executor,
                         check_lost_patch_recovery_derives_capture, check_lost_patch_missing_baseline_noop,
                         check_lost_patch_existing_capture_noop, check_lost_patch_job_recovery_surfaces_sidecar,
                         check_lost_patch_git_failure_preserves_metadata)
    for lost_patch_check in lost_patch_checks:
        lost_patch_check()
    print(f"{len(lost_patch_checks)} passed")
