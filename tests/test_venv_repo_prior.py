"""Offline checks for virtual-environment repository priors."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-venv-repo-prior-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import conv


repo_prior_cases = (
    ("/workspace/medical-app/.venv", "medical-app"),
    ("/Users/x/Documents/claude/llm-spendguard/.venv.nosync", "llm-spendguard"),
    ("/Users/x/.venvs/prelmm2", ""),
    ("/opt/shared/.venv-tools", ""),
    ("/workspace/MEDICAL-APP/.venv", "medical-app"),
    (None, ""),
    ("", ""),
)

failures = 0
for venv_path, expected_prior in repo_prior_cases:
    actual_prior = conv.repo_prior_from_venv(venv_path)
    passed = actual_prior == expected_prior
    print(("  [OK] " if passed else "  [FAIL] ")
          + f"repo prior for {venv_path!r} is {expected_prior!r}")
    failures += 0 if passed else 1

resolved = conv.resolve({"venv": "/workspace/medical-app/.venv"}, segs=[], store={})
resolve_passed = (resolved["project"] == "medical-app"
                  and resolved["source"] == "prior"
                  and resolved["how"] == "venv-prior")
print(("  [OK] " if resolve_passed else "  [FAIL] ")
      + "resolve uses the venv repository prior offline")
failures += 0 if resolve_passed else 1

ambiguous_resolved = conv.resolve({"venv": "/opt/shared/.venv-tools"}, segs=[], store={})
ambiguous_resolve_passed = ambiguous_resolved["how"] != "venv-prior"
print(("  [OK] " if ambiguous_resolve_passed else "  [FAIL] ")
      + "resolve does not use an ambiguous venv basename as a prior")
failures += 0 if ambiguous_resolve_passed else 1

print(("[OK]" if not failures else "[FAIL]")
      + " venv repository prior: %d failure(s)" % failures)
sys.exit(1 if failures else 0)
