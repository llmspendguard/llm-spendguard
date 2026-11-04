"""Git-backed edit isolation for every governed agentic provider."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

DELEGATE_WORKTREE_DIRECTORY = "delegate-worktrees"
DELEGATE_WORKTREE_METADATA_FILENAME = "worktree.json"


def _delegate_worktree_git(cwd, *arguments, input_bytes=None):
    completed = subprocess.run(["git", "-C", os.fspath(cwd), *arguments], input=input_bytes,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode:
        raise RuntimeError(f"delegate worktree git {' '.join(arguments)} failed: "
                           f"{completed.stderr.decode(errors='replace')}")
    return completed.stdout


def delegate_worktree_metadata_path(envelope_path):
    return Path(envelope_path).parent / DELEGATE_WORKTREE_METADATA_FILENAME


def read_delegate_worktree_details(envelope_path):
    workspace_metadata = delegate_worktree_metadata_path(envelope_path)
    return {"worktree": json.loads(workspace_metadata.read_text(encoding="utf-8"))} if workspace_metadata.exists() else {}


def run_isolated_delegate(executor, cwd, *, worktree_root, metadata_writer=None):
    """Snapshot dirty inputs into a git worktree and collect a complete patch.

    The primary index and working directory are never modified. Ignored inputs stay
    excluded according to git's own policy. Validation later removes accepted
    trees via ``finalize_delegate_worktree``; every failure retains its tree.
    """
    primary = Path(cwd).expanduser().resolve()
    root = Path(_delegate_worktree_git(primary, "rev-parse", "--show-toplevel").decode().strip())
    relative_cwd = primary.relative_to(root)
    parent = Path(worktree_root).expanduser()
    parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(dir=parent))
    workspace.rmdir()
    metadata = {"path": os.fspath(workspace), "primary_cwd": os.fspath(primary), "retained": True}
    def persist_delegate_worktree_metadata():
        if metadata_writer:
            metadata_writer(metadata)
    try:
        _delegate_worktree_git(root, "worktree", "add", "--detach", os.fspath(workspace), "HEAD")
        persist_delegate_worktree_metadata()
        dirty_patch = _delegate_worktree_git(root, "diff", "--binary", "HEAD", "--")
        if dirty_patch:
            _delegate_worktree_git(workspace, "apply", "--binary", "-", input_bytes=dirty_patch)
        untracked = _delegate_worktree_git(root, "ls-files", "--others", "--exclude-standard", "-z")
        for raw_path in untracked.split(b"\0"):
            if not raw_path:
                continue
            relative_path = Path(os.fsdecode(raw_path))
            destination = workspace / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = root / relative_path
            if source.is_symlink():
                destination.symlink_to(os.readlink(source))
            else:
                shutil.copy2(source, destination)
        _delegate_worktree_git(workspace, "add", "-A")
        baseline = _delegate_worktree_git(workspace, "write-tree").decode().strip()
        execution = executor(os.fspath(workspace / relative_cwd))
        _delegate_worktree_git(workspace, "add", "-A")
        metadata["patch"] = _delegate_worktree_git(
            workspace, "diff", "--cached", "--binary", baseline, "--").decode()
        metadata["changed_files"] = [os.fsdecode(path) for path in _delegate_worktree_git(
            workspace, "diff", "--cached", "--name-only", "-z", baseline, "--").split(b"\0") if path]
        persist_delegate_worktree_metadata()
        return {**execution, "worktree": metadata} if isinstance(execution, dict) else execution
    except BaseException:
        if workspace.is_dir():
            persist_delegate_worktree_metadata()
        raise


def finalize_delegate_worktree(execution, *, metadata_writer=None, envelope_writer=None):
    """Remove an accepted result's tree only after full result validation."""
    metadata = execution.get("worktree")
    if not metadata or not metadata.get("retained"):
        return
    _delegate_worktree_git(metadata["primary_cwd"], "worktree", "remove", "--force", metadata["path"])
    metadata["retained"] = False
    if metadata_writer:
        metadata_writer(metadata)
    if envelope_writer:
        envelope_writer(execution)
