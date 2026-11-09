"""Prove a clean offline wheel contains exactly the recursive source module set."""
from pathlib import Path
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE_DIRECTORY = REPOSITORY_ROOT / "src" / "spendguard"
BUILD_DIRECTORY = REPOSITORY_ROOT / "build"
WHEEL_ARCHIVE_SUFFIX = ".whl"
PYTHON_MODULE_SUFFIX = ".py"
OFFLINE_BUILD_SKIP_MARKER = "[SKIP] unbuildable offline — run in CI"
REQUIRED_OFFLINE_BUILD_MODULES = ("pip", "setuptools", "wheel")
RED_PROOF_STALE_STEM_SUFFIX = "_wheel_conformance_stale_probe"


def collect_recursive_source_module_paths():
    """Return every Python module below the source package, relative to that package."""
    return {
        module_path.relative_to(SOURCE_PACKAGE_DIRECTORY)
        for module_path in SOURCE_PACKAGE_DIRECTORY.rglob(f"*{PYTHON_MODULE_SUFFIX}")
        if "__pycache__" not in module_path.parts
    }


def collect_built_wheel_module_paths(wheel_path):
    """Return every packaged Python module below spendguard, relative to that package."""
    with zipfile.ZipFile(wheel_path) as wheel_archive:
        wheel_entries = wheel_archive.namelist()
    package_prefix = f"{SOURCE_PACKAGE_DIRECTORY.name}/"
    return {
        Path(entry.removeprefix(package_prefix))
        for entry in wheel_entries
        if entry.startswith(package_prefix) and entry.endswith(PYTHON_MODULE_SUFFIX)
    }


def assert_wheel_source_module_sets_equal(source_modules, wheel_modules):
    """Reject every missing source module and every stale wheel-only module."""
    missing_from_wheel = source_modules - wheel_modules
    stale_in_wheel = wheel_modules - source_modules
    assert not missing_from_wheel and not stale_in_wheel, (
        "wheel/source module mismatch; "
        f"missing_from_wheel={sorted(path.as_posix() for path in missing_from_wheel)!r}; "
        f"stale_in_wheel={sorted(path.as_posix() for path in stale_in_wheel)!r}"
    )


def derive_absent_red_proof_module(source_modules):
    """Derive a wheel-only module name without assuming any package or module inventory."""
    seed_module = min(source_modules)
    candidate = seed_module.with_stem(seed_module.stem + RED_PROOF_STALE_STEM_SUFFIX)
    while candidate in source_modules:
        candidate = candidate.with_stem(candidate.stem + RED_PROOF_STALE_STEM_SUFFIX)
    return candidate


def prove_wheel_conformance_check_rejects_stale_module(source_modules):
    """RED proof: an injected stale wheel member must fail and name the gap."""
    injected_stale_module = derive_absent_red_proof_module(source_modules)
    try:
        assert_wheel_source_module_sets_equal(source_modules, source_modules | {injected_stale_module})
    except AssertionError as error:
        assert injected_stale_module.as_posix() in str(error), str(error)
        print(f"[PASS] RED proof rejected stale module: {injected_stale_module.as_posix()}")
        return
    raise AssertionError("wheel conformance assertion passed despite an injected stale module")


def offline_build_tooling_is_available():
    """Report whether this interpreter already has every tool needed without network access."""
    return all(importlib.util.find_spec(module_name) is not None
               for module_name in REQUIRED_OFFLINE_BUILD_MODULES)


def build_clean_offline_wheel(wheel_directory, quarantine_directory):
    """Quarantine stale setuptools output and build one wheel without isolation or network."""
    quarantined_build = quarantine_directory / BUILD_DIRECTORY.name
    build_was_present = BUILD_DIRECTORY.exists()
    if build_was_present:
        BUILD_DIRECTORY.replace(quarantined_build)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "--wheel-dir", str(wheel_directory), str(REPOSITORY_ROOT)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, (
            f"offline wheel build failed with exit {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    finally:
        shutil.rmtree(BUILD_DIRECTORY, ignore_errors=True)
        if build_was_present:
            quarantined_build.replace(BUILD_DIRECTORY)
    wheel_paths = tuple(wheel_directory.glob(f"*{WHEEL_ARCHIVE_SUFFIX}"))
    assert len(wheel_paths) == 1, f"expected exactly one built wheel, found {[path.name for path in wheel_paths]!r}"
    assert wheel_paths[0].stat().st_size > 0, f"built wheel is empty: {wheel_paths[0]}"
    return wheel_paths[0]


def run_wheel_source_conformance_proof():
    """Run the RED mutation proof and the clean real-wheel equality proof."""
    source_modules = collect_recursive_source_module_paths()
    assert source_modules, f"source module set is empty: {SOURCE_PACKAGE_DIRECTORY}"
    prove_wheel_conformance_check_rejects_stale_module(source_modules)
    if not offline_build_tooling_is_available():
        print(f"{OFFLINE_BUILD_SKIP_MARKER}: missing "
              f"{[name for name in REQUIRED_OFFLINE_BUILD_MODULES if importlib.util.find_spec(name) is None]!r}")
        return
    with tempfile.TemporaryDirectory(prefix="spendguard-wheel-conformance-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        wheel_path = build_clean_offline_wheel(temporary_root / "wheels", temporary_root / "quarantine")
        wheel_modules = collect_built_wheel_module_paths(wheel_path)
        assert wheel_modules, f"built wheel contains no {SOURCE_PACKAGE_DIRECTORY.name} Python modules: {wheel_path}"
        assert_wheel_source_module_sets_equal(source_modules, wheel_modules)
    print(f"[PASS] clean wheel exactly matches all {len(source_modules)} recursive source modules")


if __name__ == "__main__":
    run_wheel_source_conformance_proof()
