"""Offline regression guards for deterministic, registry-complete coverage discovery."""
import contextlib
import io
import os
import sys
import tempfile
from unittest import mock

from spendguard import coverage, fleet


failures = 0


def check_coverage_discovery(label, condition):
    print("  [%s] %s" % ("OK" if condition else "FAIL", label))
    return 0 if condition else 1


with tempfile.TemporaryDirectory(prefix="coverage-cwd-a-") as first_cwd, \
        tempfile.TemporaryDirectory(prefix="coverage-cwd-b-") as second_cwd:
    with mock.patch.object(fleet, "registered_interpreters", return_value=[]), \
            mock.patch.object(os, "getcwd", return_value=os.path.join(first_cwd, "work")):
        first_roots = set(coverage._default_roots())
    with mock.patch.object(fleet, "registered_interpreters", return_value=[]), \
            mock.patch.object(os, "getcwd", return_value=os.path.join(second_cwd, "work")):
        second_roots = set(coverage._default_roots())
    failures += check_coverage_discovery("default roots are independent of cwd", first_roots == second_roots)


with tempfile.TemporaryDirectory(prefix="coverage-registry-home-") as registry_home, \
        tempfile.TemporaryDirectory(prefix="coverage-unrelated-cwd-") as unrelated_cwd:
    registered_venv = os.path.join(registry_home, "registered-project", ".venv")
    registry_helper = getattr(coverage, "_registry_venv_roots", None)
    candidate_helper = getattr(coverage, "_candidate_venvs", None)
    if registry_helper is None or candidate_helper is None:
        registry_discovery_complete = False
    else:
        with mock.patch.object(coverage, "_registry_venv_roots", return_value=([registered_venv], True)), \
                mock.patch.object(os, "getcwd", return_value=unrelated_cwd):
            expected_glob = os.path.join(os.path.dirname(os.path.dirname(registered_venv)), "*", ".venv*")
            registry_discovery_complete = (
                expected_glob in coverage._default_roots()
                and registered_venv in coverage._candidate_venvs(None)
            )
    failures += check_coverage_discovery(
        "registry roots seed scan homes and direct candidates", registry_discovery_complete
    )


with tempfile.TemporaryDirectory(prefix="coverage-sibling-repo-") as sibling_home, \
        tempfile.TemporaryDirectory(prefix="coverage-sibling-cwd-") as sibling_cwd:
    registered_venv = os.path.join(sibling_home, "container", "registered_repo", ".venv")
    sibling_venv = os.path.join(sibling_home, "container", "other_repo", ".venv")
    os.makedirs(registered_venv)
    os.makedirs(sibling_venv)
    with mock.patch.object(coverage, "_registry_venv_roots", return_value=([registered_venv], True)), \
            mock.patch.object(os, "getcwd", return_value=sibling_cwd):
        expected_glob = os.path.join(sibling_home, "container", "*", ".venv*")
        sibling_repo_discovered = (
            expected_glob in coverage._default_roots()
            and sibling_venv in coverage._candidate_venvs(None)
        )
    failures += check_coverage_discovery(
        "unregistered sibling repo venv is enumerated via registry-seeded container glob",
        sibling_repo_discovered,
    )


registry_helper = getattr(coverage, "_registry_venv_roots", None)
if registry_helper is None:
    unavailable_registry_degrades = False
else:
    with mock.patch.object(fleet, "registered_interpreters", side_effect=RuntimeError("registry unavailable")):
        try:
            registry_roots, registry_readable = coverage._registry_venv_roots()
            unavailable_registry_degrades = (
                registry_roots == []
                and not registry_readable
                and "~/.venvs/*" in coverage._default_roots()
            )
        except Exception:
            unavailable_registry_degrades = False
failures += check_coverage_discovery(
    "unavailable registry degrades to fixed glob discovery", unavailable_registry_degrades
)


registry_patch_target = getattr(coverage, "_registry_venv_roots", None)
with tempfile.TemporaryDirectory(prefix="coverage-unused-venv-") as unused_venv:
    if registry_patch_target is None:
        explicit_empty_scans_nothing = coverage.audit_gating_coverage(roots=[]) == []
    else:
        with mock.patch.object(coverage, "_registry_venv_roots", return_value=([unused_venv], True)):
            explicit_empty_scans_nothing = coverage.audit_gating_coverage(roots=[]) == []
failures += check_coverage_discovery(
    "explicit roots=[] does not include registry candidates", explicit_empty_scans_nothing
)


registry_failure = iter((RuntimeError("registry unavailable"),))


def fail_first_registry_read_then_return_empty():
    try:
        raise next(registry_failure)
    except StopIteration:
        return []


coverage_output = io.StringIO()
with mock.patch.object(fleet, "registered_interpreters", side_effect=fail_first_registry_read_then_return_empty), \
        mock.patch.object(coverage, "audit_gating_coverage", return_value=[]), \
        mock.patch.object(coverage, "_captured_spend_sources", return_value=[]), \
        contextlib.redirect_stdout(coverage_output):
    coverage.cmd()
failures += check_coverage_discovery(
    "cmd preserves the authoritative registry-read failure warning",
    "registry unreadable" in coverage_output.getvalue(),
)


print(("[OK]" if not failures else "[FAIL]")
      + " coverage discovery determinism: %d failure(s)" % failures)
sys.exit(1 if failures else 0)
