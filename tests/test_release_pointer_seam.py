"""Mutation guards for the release-pointer writer/reader contract."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import distribution, release


TEST_GIT_AUTHOR_NAME = "Release Pointer Test"
TEST_GIT_AUTHOR_EMAIL = "release-pointer-test@example.invalid"
TEST_INITIAL_COMMIT_MESSAGE = "initial release"
TEST_HEAD_COMMIT_MESSAGE = "later release"
TEST_RELEASE_TAG = "release-pointer-test-tag"
TEST_VERSION_SYMBOL = "__version__"
TEST_INITIAL_VERSION = "1.0.0"
TEST_HEAD_VERSION = "2.0.0"
TEST_POINTER_BYTES = b'{"sha":"preserved-pointer"}\n'


def initialize_release_pointer_test_repository(repository: Path) -> tuple[str, str]:
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", TEST_GIT_AUTHOR_NAME], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", TEST_GIT_AUTHOR_EMAIL], check=True)
    version_file = repository / "src" / "spendguard" / "_version.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f"{TEST_VERSION_SYMBOL} = {TEST_INITIAL_VERSION!r}\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", TEST_INITIAL_COMMIT_MESSAGE], check=True)
    initial_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repository), "tag", TEST_RELEASE_TAG, initial_sha], check=True)
    version_file.write_text(f"{TEST_VERSION_SYMBOL} = {TEST_HEAD_VERSION!r}\n")
    subprocess.run(["git", "-C", str(repository), "commit", "-qam", TEST_HEAD_COMMIT_MESSAGE], check=True)
    head_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return initial_sha, head_sha


def commit_distribution_version_tree(repository: Path, relative_files: dict[Path, str]) -> str:
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", TEST_GIT_AUTHOR_NAME], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", TEST_GIT_AUTHOR_EMAIL], check=True)
    for relative_path, source in relative_files.items():
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", TEST_INITIAL_COMMIT_MESSAGE], check=True)
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


fails = []


with tempfile.TemporaryDirectory(prefix="release-pointer-writer-") as temporary_directory:
    temporary_root = Path(temporary_directory)
    repository = temporary_root / "repository"
    _initial_sha, head_sha = initialize_release_pointer_test_repository(repository)
    pointer = temporary_root / "state" / release.POINTER_NAME
    pointer.parent.mkdir()
    pointer.write_bytes(TEST_POINTER_BYTES)
    nonexistent_sha = ("0" if head_sha[0] != "0" else "1") + head_sha[1:]
    refusal = None
    with patch.object(release, "_src_root", lambda: repository), patch.object(
            release, "pointer_path", lambda: pointer):
        try:
            release.promote_release(nonexistent_sha)
        except ValueError as exc:
            refusal = exc
    record_script_check(
        fails,
        "well-formed nonexistent commit is refused before the pointer changes",
        refusal is not None and "git cannot resolve" in str(refusal) and pointer.read_bytes() == TEST_POINTER_BYTES,
    )

with tempfile.TemporaryDirectory(prefix="release-pointer-describe-") as temporary_directory:
    temporary_root = Path(temporary_directory)
    repository = temporary_root / "repository"
    initial_sha, _head_sha = initialize_release_pointer_test_repository(repository)
    pointer = temporary_root / "state" / release.POINTER_NAME
    with patch.object(release, "_src_root", lambda: repository), patch.object(
            release, "pointer_path", lambda: pointer):
        promoted = release.promote_release(initial_sha)
    record_script_check(
        fails, "default describe names the promoted non-HEAD commit", promoted["describe"] == TEST_RELEASE_TAG
    )

with tempfile.TemporaryDirectory(prefix="release-pointer-symbolic-") as temporary_directory:
    temporary_root = Path(temporary_directory)
    repository = temporary_root / "repository"
    initial_sha, head_sha = initialize_release_pointer_test_repository(repository)
    pointer = temporary_root / "state" / release.POINTER_NAME
    revisions = {"head": "HEAD", "tag": TEST_RELEASE_TAG, "short_sha": initial_sha[:12]}
    expected_shas = {"head": head_sha, "tag": initial_sha, "short_sha": initial_sha}
    with patch.object(release, "_src_root", lambda: repository), patch.object(
            release, "pointer_path", lambda: pointer):
        promoted_shas = {revision_kind: release.promote_release(revision)["sha"]
                         for revision_kind, revision in revisions.items()}
    record_script_check(
        fails, "HEAD, tag, and abbreviated commit revisions still promote", promoted_shas == expected_shas
    )

with tempfile.TemporaryDirectory(prefix="release-pointer-reader-layout-") as temporary_directory:
    temporary_root = Path(temporary_directory)
    repository = temporary_root / "repository"
    package_root = Path(*distribution.__package__.split("."))
    nested_version_path = Path("src") / package_root / "platform" / "release_identity.py"
    blessed_sha = commit_distribution_version_tree(
        repository, {nested_version_path: f"{TEST_VERSION_SYMBOL} = {TEST_INITIAL_VERSION!r}\n"}
    )
    resolved_target = None
    resolution_error = None
    with patch.object(release, "_src_root", lambda: repository), patch.object(
            release, "green_pointer", lambda: {"sha": blessed_sha}):
        try:
            resolved_target = distribution.resolve_distribution_target()
        except ValueError as exc:
            resolution_error = exc
    record_script_check(
        fails,
        "blessed-tree version resolves when its module path differs from the running layout",
        resolution_error is None and resolved_target["green_version"] == TEST_INITIAL_VERSION,
    )

for declaration_count in (0, 2):
    with tempfile.TemporaryDirectory(prefix="release-pointer-reader-unique-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        repository = temporary_root / "repository"
        package_root = Path("src") / Path(*distribution.__package__.split("."))
        sources = {
            package_root / f"candidate_{candidate_index}.py": f"{TEST_VERSION_SYMBOL} = {TEST_INITIAL_VERSION!r}\n"
            for candidate_index in range(declaration_count)
        }
        if not sources:
            sources[package_root / "package_marker.py"] = "PACKAGE_MARKER = True\n"
        blessed_sha = commit_distribution_version_tree(repository, sources)
        refusal = None
        with patch.object(release, "_src_root", lambda release_repository=repository: release_repository), patch.object(
                release, "green_pointer", lambda release_sha=blessed_sha: {"sha": release_sha}):
            try:
                distribution.resolve_distribution_target()
            except ValueError as exc:
                refusal = exc
        record_script_check(
            fails,
            f"blessed tree with {declaration_count} version declarations is refused",
            refusal is not None and "deployed version is not uniquely established" in str(refusal),
        )

print(f"{'[FAIL]' if fails else 'OK'} test_release_pointer_seam: {len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
