"""Git-backed edit isolation for every governed agentic provider."""
import json
import os
import datetime
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

DELEGATE_WORKTREE_DIRECTORY = "delegate-worktrees"
DELEGATE_WORKTREE_METADATA_FILENAME = "worktree.json"
DELEGATE_WORKTREE_DEFAULT_RETENTION_S = 1500
# Orphans have no authoritative deadline, so grant twice the normal inspection
# floor before reclaiming them to avoid shortening retention after journal loss.
DELEGATE_WORKTREE_DEFAULT_ORPHAN_GRACE_S = DELEGATE_WORKTREE_DEFAULT_RETENTION_S * 2
DELEGATE_WORKTREE_GITDIR_PREFIX = "gitdir: "
DELEGATE_WORKTREE_GITDIR_MARKER = "/.git/worktrees/"


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


def write_delegate_worktree_details(envelope_path, metadata):
    from ... import config
    config.update_json(delegate_worktree_metadata_path(envelope_path),
                       lambda previous: metadata, reason="persist isolated delegate workspace", required=True)


def _delegate_worktree_reclaim_timestamp(now, retention_s):
    current = now or datetime.datetime.now(datetime.timezone.utc)
    return (current + datetime.timedelta(seconds=float(retention_s))).isoformat(timespec="seconds")


def run_isolated_delegate(executor, cwd, *, worktree_root, metadata_writer=None, now=None,
                          retention_s=DELEGATE_WORKTREE_DEFAULT_RETENTION_S):
    """Snapshot dirty inputs into a git worktree and collect a complete patch.

    The primary index and working directory are never modified. Ignored inputs stay
    excluded according to git's own policy. Validation later removes accepted
    trees only after the configured retention floor; every failure receives the
    same inspectable retention window.
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
        metadata["baseline_tree"] = baseline
        persist_delegate_worktree_metadata()
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
            metadata["reclaim_not_before"] = _delegate_worktree_reclaim_timestamp(now, retention_s)
            persist_delegate_worktree_metadata()
        raise


def recover_retained_delegate_worktree_patch(metadata, *, now=None,
                                             retention_s=DELEGATE_WORKTREE_DEFAULT_RETENTION_S):
    """Derive a lost owner's patch from its retained worktree and persisted baseline.

    Stamp a reclaim deadline after capture. Return metadata unchanged when recovery
    is unnecessary, unavailable, or fails.
    """
    if (not metadata.get("retained") or metadata.get("patch")
            or not metadata.get("baseline_tree") or not metadata.get("path")):
        return metadata
    try:
        workspace = Path(metadata["path"]).expanduser()
        if not workspace.is_dir():
            return metadata
        _delegate_worktree_git(workspace, "add", "-A")
        patch = _delegate_worktree_git(
            workspace, "diff", "--cached", "--binary", metadata["baseline_tree"], "--").decode()
        changed_files = [os.fsdecode(path) for path in _delegate_worktree_git(
            workspace, "diff", "--cached", "--name-only", "-z", metadata["baseline_tree"], "--").split(b"\0") if path]
        reclaim_not_before = metadata.get("reclaim_not_before") or _delegate_worktree_reclaim_timestamp(now, retention_s)
    except Exception as error:
        print(f"[spendguard] recover_retained_delegate_worktree_patch: {error}", file=sys.stderr, flush=True)
        return metadata
    metadata.update(patch=patch, changed_files=changed_files, reclaim_not_before=reclaim_not_before)
    return metadata


def schedule_delegate_worktree_reclaim(execution, *, metadata_writer=None, envelope_writer=None,
                                       now=None, retention_s=DELEGATE_WORKTREE_DEFAULT_RETENTION_S):
    """Stamp the retention floor; periodic reap cadence determines the ceiling."""
    metadata = execution.get("worktree")
    if not metadata or not metadata.get("retained"):
        return
    metadata["reclaim_not_before"] = _delegate_worktree_reclaim_timestamp(now, retention_s)
    if metadata_writer:
        metadata_writer(metadata)
    if envelope_writer:
        envelope_writer(execution)


def _delegate_worktree_primary_cwd_from_gitfile(worktree_path):
    gitfile = Path(worktree_path) / ".git"
    content = gitfile.read_text(encoding="utf-8").strip()
    if not content.startswith(DELEGATE_WORKTREE_GITDIR_PREFIX):
        raise ValueError("worktree .git file has no gitdir prefix")
    gitdir = content[len(DELEGATE_WORKTREE_GITDIR_PREFIX):]
    marker_index = gitdir.rfind(DELEGATE_WORKTREE_GITDIR_MARKER)
    if marker_index < 0:
        raise ValueError("worktree gitdir has no registered-worktree marker")
    return Path(gitdir[:marker_index])


def reclaim_expired_delegate_worktrees(now=None, *, journal_root_resolver, metadata_updater, worktree_root,
                                       orphan_grace_s=DELEGATE_WORKTREE_DEFAULT_ORPHAN_GRACE_S):
    """Reclaim after the TTL floor; scheduled invocation frequency sets the ceiling.

    Sidecar deadlines are authoritative. Journal-less directories receive the
    longer orphan grace because no authoritative deadline survived.
    """
    report = {"reclaimed": 0, "retained": 0, "gaps": []}
    root = Path(worktree_root).expanduser()
    if not root.is_dir():
        return report
    current = now or datetime.datetime.now(datetime.timezone.utc)
    journal_root = Path(journal_root_resolver()).expanduser()
    governed_paths = set()
    # Warm executions may use an alternate journal root; their sidecars are intentionally absent here,
    # so their worktrees fall through to the longer orphan-grace path rather than becoming unreclaimable.
    sidecars = journal_root.glob(f"*/{DELEGATE_WORKTREE_METADATA_FILENAME}") if journal_root.is_dir() else ()
    for sidecar in sidecars:
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            worktree_path = Path(metadata["path"]).expanduser()
            governed_paths.add(worktree_path.resolve())
            if not metadata.get("retained"):
                continue
            deadline_text = metadata.get("reclaim_not_before")
            if not deadline_text:
                report["gaps"].append({"path": os.fspath(worktree_path),
                                       "reason": "retained sidecar has no reclaim deadline"})
                continue
            deadline = datetime.datetime.fromisoformat(deadline_text)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            if current < deadline:
                report["retained"] += 1
                continue
            _delegate_worktree_git(metadata["primary_cwd"], "worktree", "remove", "--force",
                                   os.fspath(worktree_path))
            metadata["retained"] = False
            metadata_updater(sidecar, lambda previous, updated=metadata: updated,
                             reason="persist reclaimed delegate workspace", required=True)
            report["reclaimed"] += 1
        except Exception as error:
            report["gaps"].append({"path": os.fspath(sidecar), "reason": str(error)})
    orphan_cutoff = current.timestamp() - float(orphan_grace_s)
    for worktree_path in root.iterdir():
        try:
            if not worktree_path.is_dir() or worktree_path.resolve() in governed_paths:
                continue
            if worktree_path.stat().st_mtime > orphan_cutoff:
                report["retained"] += 1
                continue
            primary_cwd = _delegate_worktree_primary_cwd_from_gitfile(worktree_path)
            _delegate_worktree_git(primary_cwd, "worktree", "remove", "--force", os.fspath(worktree_path))
            report["reclaimed"] += 1
        except Exception as error:
            report["gaps"].append({"path": os.fspath(worktree_path), "reason": str(error)})
    return report
