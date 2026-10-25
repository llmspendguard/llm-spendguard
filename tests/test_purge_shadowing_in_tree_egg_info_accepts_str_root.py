"""Regression: editable-package metadata purge accepts install-hook's string repo root."""
import os
import tempfile
from pathlib import Path

from spendguard import setup


FAKE_EGG_INFO_DIRECTORY_NAME = "offline-consumer.egg-info"


with tempfile.TemporaryDirectory(prefix="spendguard-purge-str-root-") as scratch_directory:
    fake_repo_root = Path(scratch_directory)
    fake_egg_info_directory = fake_repo_root / "src" / FAKE_EGG_INFO_DIRECTORY_NAME
    fake_egg_info_directory.mkdir(parents=True)

    removed_metadata_directories = setup.purge_shadowing_in_tree_egg_info(os.fspath(fake_repo_root))

    assert removed_metadata_directories == [os.fspath(fake_egg_info_directory.resolve())]
    assert not fake_egg_info_directory.exists()

print("[OK] purge accepts install-hook's string repo root and removes in-tree egg-info")
