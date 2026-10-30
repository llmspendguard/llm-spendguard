"""Real git worktrees must contain delegated edits and preserve dirty inputs."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
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
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="delegate-worktree-contract-")
from spendguard import delegate_worktrees
from spendguard import codex_exec, delegate_jobs, delegate_router, execution_journal
delegate_router.config.HOME = Path(os.environ["SPENDGUARD_HOME"])


def exercise_delegate_worktree_contract():
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / WORKTREE_FIXTURE_REPO_NAME
        repo.mkdir()
        def git_fixture_command(*arguments):
            return subprocess.check_output(["git", "-C", str(repo), *arguments])
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
        def edit_isolated_fixture(path):
            workspace = Path(path)
            assert workspace != repo
            assert (workspace / source.name).read_text() == source.read_text()
            assert (workspace / WORKTREE_FIXTURE_UNTRACKED_NAME).read_text() == WORKTREE_FIXTURE_UNTRACKED_TEXT
            (workspace / source.name).write_text(WORKTREE_FIXTURE_OUTPUT_TEXT)
            (workspace / WORKTREE_FIXTURE_BINARY_NAME).write_bytes(bytes(range(WORKTREE_FIXTURE_BYTE_VALUES)))
            if simultaneous_edits is not None:
                simultaneous_edits.wait()
            assert source.read_text() == WORKTREE_FIXTURE_DIRTY_TEXT
            return {"text": "complete"}
        def provider_edits_isolated_fixture(prompt, **kwargs):
            assert kwargs["sandbox"] == "workspace-write"
            return edit_isolated_fixture(kwargs["cwd"])
        with patch.object(codex_exec, "run_prompt", provider_edits_isolated_fixture):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = list(pool.map(lambda _: delegate_router._route_agentic(
                    WORKTREE_FIXTURE_PROVIDER, WORKTREE_FIXTURE_PROMPT, WORKTREE_FIXTURE_MODEL,
                    None, str(repo)), range(WORKTREE_FIXTURE_PARALLEL_COUNT)))
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
        subprocess.check_call(["git", "-C", str(repo), "worktree", "remove", "--force", failed["worktree"]["path"]])
        try:
            delegate_router._run_isolated_agentic_provider(edit_isolated_fixture, Path(temporary))
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-git workspace silently accepted")
    print("D17 worktree isolation contracts: PASS")


if __name__ == "__main__":
    exercise_delegate_worktree_contract()
