"""Real git worktrees must contain delegated edits and preserve dirty inputs."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import sys
from unittest.mock import patch

WORKTREE_FIXTURE_REPO_NAME = "repository"
WORKTREE_FIXTURE_EMAIL = "fixture@example.invalid"
WORKTREE_FIXTURE_AUTHOR = "Worktree fixture"
WORKTREE_FIXTURE_SOURCE_NAME = "source.txt"
WORKTREE_FIXTURE_UNTRACKED_NAME = "untracked.txt"
WORKTREE_FIXTURE_BINARY_NAME = "new.bin"
WORKTREE_FIXTURE_COMMITTED_TEXT = "committed\n"
WORKTREE_FIXTURE_DIRTY_TEXT = "dirty input\n"
WORKTREE_FIXTURE_UNTRACKED_TEXT = "untracked input\n"
WORKTREE_FIXTURE_OUTPUT_TEXT = "delegated output\n"
WORKTREE_FIXTURE_PARALLEL_COUNT = 2
WORKTREE_FIXTURE_BYTE_VALUES = 256
WORKTREE_FIXTURE_INTENT = "worktree-lifecycle"
WORKTREE_FIXTURE_PROVIDER = "codex"
WORKTREE_FIXTURE_MODEL = "fixture-model"
WORKTREE_FIXTURE_PROMPT = "fixture"
WORKTREE_FIXTURE_COMMIT_MESSAGE = "baseline"
WORKTREE_FIXTURE_FAILURE = "fixture failure"
# Per-operation fixture budget; barrier and guard budgets reserve room for cleanup.
WORKTREE_FIXTURE_OPERATION_BUDGET_S = 30
WORKTREE_FIXTURE_BARRIER_BUDGET_S = WORKTREE_FIXTURE_OPERATION_BUDGET_S / WORKTREE_FIXTURE_PARALLEL_COUNT
WORKTREE_FIXTURE_GUARD_BUDGET_S = WORKTREE_FIXTURE_BARRIER_BUDGET_S / WORKTREE_FIXTURE_PARALLEL_COUNT
WORKTREE_FIXTURE_FAILURE_FLAG = "--fail-worker"
WORKTREE_FIXTURE_MISSING_PEER_FLAG = "--missing-peer"
WORKTREE_FIXTURE_TIMEOUT_PROBE_SCALE = 100
WORKTREE_FIXTURE_BARRIER_ERROR = "parallel worktree edits: peer failed or barrier timed out"
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="delegate-worktree-contract-")
from spendguard import delegate_worktrees
from spendguard import codex_exec, delegate_jobs, delegate_router, execution_journal
delegate_router.config.HOME = Path(os.environ["SPENDGUARD_HOME"])


def exercise_delegate_worktree_contract(*, fail_worker=False, missing_peer=False):
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / WORKTREE_FIXTURE_REPO_NAME
        repo.mkdir()
        def git_fixture_command(*arguments):
            return subprocess.check_output(["git", "-C", str(repo), *arguments],
                                           timeout=WORKTREE_FIXTURE_OPERATION_BUDGET_S)
        git_fixture_command("init")
        git_fixture_command("config", "user.email", WORKTREE_FIXTURE_EMAIL)
        git_fixture_command("config", "user.name", WORKTREE_FIXTURE_AUTHOR)
        source = repo / WORKTREE_FIXTURE_SOURCE_NAME
        source.write_text(WORKTREE_FIXTURE_COMMITTED_TEXT)
        git_fixture_command("add", ".")
        git_fixture_command("commit", "-m", WORKTREE_FIXTURE_COMMIT_MESSAGE)
        source.write_text(WORKTREE_FIXTURE_DIRTY_TEXT)
        (repo / WORKTREE_FIXTURE_UNTRACKED_NAME).write_text(WORKTREE_FIXTURE_UNTRACKED_TEXT)
        simultaneous_edits = threading.Barrier(WORKTREE_FIXTURE_PARALLEL_COUNT)
        peer_editing = threading.Event()
        worktree_add_lock = threading.Lock()
        original_git = delegate_worktrees._delegate_worktree_git
        original_subprocess_run = subprocess.run
        def bounded_worktree_fixture_command(*args, **kwargs):
            kwargs.setdefault("timeout", WORKTREE_FIXTURE_OPERATION_BUDGET_S)
            return original_subprocess_run(*args, **kwargs)
        def serialize_fixture_worktree_add(cwd, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "add"):
                if not worktree_add_lock.acquire(timeout=WORKTREE_FIXTURE_OPERATION_BUDGET_S):
                    raise AssertionError("worktree creation lock timed out")
                try:
                    return original_git(cwd, *arguments, **kwargs)
                finally:
                    worktree_add_lock.release()
            return original_git(cwd, *arguments, **kwargs)
        def edit_isolated_fixture(path):
            workspace = Path(path)
            assert workspace != repo
            assert (workspace / source.name).read_text() == source.read_text()
            assert (workspace / WORKTREE_FIXTURE_UNTRACKED_NAME).read_text() == WORKTREE_FIXTURE_UNTRACKED_TEXT
            (workspace / source.name).write_text(WORKTREE_FIXTURE_OUTPUT_TEXT)
            (workspace / WORKTREE_FIXTURE_BINARY_NAME).write_bytes(bytes(range(WORKTREE_FIXTURE_BYTE_VALUES)))
            if simultaneous_edits is not None:
                peer_editing.set()
                try:
                    simultaneous_edits.wait(timeout=WORKTREE_FIXTURE_BARRIER_BUDGET_S)
                except threading.BrokenBarrierError as error:
                    raise AssertionError(WORKTREE_FIXTURE_BARRIER_ERROR) from error
            assert source.read_text() == WORKTREE_FIXTURE_DIRTY_TEXT
            return {"text": "complete"}
        def provider_edits_isolated_fixture(prompt, **kwargs):
            assert kwargs["sandbox"] == "workspace-write"
            return edit_isolated_fixture(kwargs["cwd"])
        def run_worktree_fixture_worker(worker_index):
            try:
                if fail_worker and worker_index == 0:
                    assert peer_editing.wait(timeout=WORKTREE_FIXTURE_BARRIER_BUDGET_S), "peer never reached edits"
                    raise RuntimeError(WORKTREE_FIXTURE_FAILURE)
                return delegate_router._route_agentic(
                    WORKTREE_FIXTURE_PROVIDER, WORKTREE_FIXTURE_PROMPT, WORKTREE_FIXTURE_MODEL,
                    None, str(repo))
            except BaseException:
                simultaneous_edits.abort()
                raise
        with patch.object(codex_exec, "run_prompt", provider_edits_isolated_fixture), patch.object(
                delegate_worktrees, "_delegate_worktree_git", serialize_fixture_worktree_add), patch.object(
                subprocess, "run", bounded_worktree_fixture_command):
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKTREE_FIXTURE_PARALLEL_COUNT) as pool:
                futures = [pool.submit(run_worktree_fixture_worker, index)
                           for index in range(1 if missing_peer else WORKTREE_FIXTURE_PARALLEL_COUNT)]
                results = [future.result(timeout=WORKTREE_FIXTURE_OPERATION_BUDGET_S) for future in futures]
        simultaneous_edits = None
        paths = [item["worktree"]["path"] for item in results]
        assert paths[0] != paths[1]
        for item in results:
            assert execution_journal.current_durable_execution_id() is None
            delegate_router._finalize_accepted_delegate_workspace(item)
            metadata_path = delegate_worktrees.delegate_worktree_metadata_path(
                execution_journal.durable_execution_payload_paths(item["execution_id"])["envelope"])
            assert json.loads(metadata_path.read_text())["retained"] is False
        assert all(not Path(path).exists() for path in paths)
        assert all(WORKTREE_FIXTURE_BINARY_NAME in item["worktree"]["changed_files"] for item in results)
        assert all("GIT binary patch" in item["worktree"]["patch"] for item in results)
        assert source.read_text() == WORKTREE_FIXTURE_DIRTY_TEXT
        delegate_jobs.config.HOME = delegate_router.config.HOME
        job_id = delegate_jobs.create_delegate_job(kind="agentic", task_fingerprint=WORKTREE_FIXTURE_INTENT,
            cwd=repo, provider=WORKTREE_FIXTURE_PROVIDER, intent=WORKTREE_FIXTURE_INTENT, dirty_before=set())
        execution_id = delegate_jobs.read_delegate_job(job_id)["execution_id"]
        def assemble_isolated_fixture(execution, **kwargs):
            delegate_router._finalize_accepted_delegate_workspace(execution)
            return {"status": "executed", "execution": execution,
                    "changed_files": kwargs["changed_files"]}
        with patch.object(codex_exec, "run_prompt", provider_edits_isolated_fixture), patch.object(
                delegate_router, "_assemble_delegate_execution_result", assemble_isolated_fixture):
            delegate_router._run_delegate_job_to_completion(job_id, WORKTREE_FIXTURE_PROVIDER, WORKTREE_FIXTURE_PROMPT,
                WORKTREE_FIXTURE_MODEL, None, str(repo), set(), result={}, classification={}, estimate={},
                intent=WORKTREE_FIXTURE_INTENT)
        collected = delegate_jobs.read_delegate_job_result(job_id)
        assert collected["state"] == "done" and WORKTREE_FIXTURE_BINARY_NAME in collected["changed_files"]
        assert delegate_jobs.read_delegate_job(job_id)["worktree"]["retained"] is False
        recovered = execution_journal.recover_durable_execution(execution_id)
        assert recovered["state"] == "success"
        assert WORKTREE_FIXTURE_BINARY_NAME in json.loads(execution_journal.durable_execution_payload_paths(
            execution_id)["envelope"].read_text())["worktree"]["changed_files"]
        failed = delegate_router._run_isolated_agentic_provider(lambda path: {"error": WORKTREE_FIXTURE_FAILURE}, repo)
        assert failed["worktree"]["retained"] and Path(failed["worktree"]["path"]).exists()
        subprocess.check_call(["git", "-C", str(repo), "worktree", "remove", "--force", failed["worktree"]["path"]],
                              timeout=WORKTREE_FIXTURE_OPERATION_BUDGET_S)
        try:
            delegate_router._run_isolated_agentic_provider(edit_isolated_fixture, Path(temporary))
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-git workspace silently accepted")
    print("D17 worktree isolation contracts: PASS")


def prove_worktree_peer_failure_is_bounded():
    """A real worker dies only after its peer reaches edits; executor shutdown must finish."""
    try:
        failure = subprocess.run([sys.executable, __file__, WORKTREE_FIXTURE_FAILURE_FLAG],
                                 capture_output=True, text=True, timeout=WORKTREE_FIXTURE_GUARD_BUDGET_S)
    except subprocess.TimeoutExpired as error:
        raise AssertionError("worker failure stranded a peer at the worktree barrier") from error
    assert failure.returncode != 0, "injected worker failure silently accepted"
    assert WORKTREE_FIXTURE_FAILURE in failure.stderr, failure.stderr
    print("D17 injected worker failure: bounded and surfaced")
    try:
        missing = subprocess.run([sys.executable, __file__, WORKTREE_FIXTURE_MISSING_PEER_FLAG],
                                 capture_output=True, text=True, timeout=WORKTREE_FIXTURE_GUARD_BUDGET_S)
    except subprocess.TimeoutExpired as error:
        raise AssertionError("missing peer stranded a worker at the worktree barrier") from error
    assert missing.returncode != 0, "missing barrier peer silently accepted"
    assert WORKTREE_FIXTURE_BARRIER_ERROR in missing.stderr, missing.stderr
    print("D17 missing peer: barrier timeout bounded and surfaced")


if __name__ == "__main__":
    if WORKTREE_FIXTURE_FAILURE_FLAG in sys.argv:
        exercise_delegate_worktree_contract(fail_worker=True)
    elif WORKTREE_FIXTURE_MISSING_PEER_FLAG in sys.argv:
        WORKTREE_FIXTURE_BARRIER_BUDGET_S = (
            WORKTREE_FIXTURE_GUARD_BUDGET_S / WORKTREE_FIXTURE_TIMEOUT_PROBE_SCALE)
        exercise_delegate_worktree_contract(missing_peer=True)
    else:
        prove_worktree_peer_failure_is_bounded()
        exercise_delegate_worktree_contract()
