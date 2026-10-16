"""CLI help is an offline, side-effect-free bootstrap path, not a command execution path."""
import json
import os
import pathlib
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKTREE_SOURCE = ROOT / "src"
HELP_CASES = (("top-level", ["--help"]), ("scan", ["scan", "--help"]),
              ("doctor", ["doctor", "--help"]))
HEAVY_STARTUP_MODULES = ("anthropic", "openai", "spendguard.crossllm", "spendguard.gate",
                         "spendguard.ledger", "spendguard.pricing")
HELP_COMPLETION_LIMIT_SECONDS = 5.0
RESULT_MARKER = "SPENDGUARD_HELP_PROBE="
FAKE_OFFLINE_OPENAI_KEY = "sk-test-cli-help-must-never-use-this-key"


def run_isolated_help_probe(arguments):
    probe = (
        "import json,sys; import spendguard.cli as cli; "
        "rc=cli.main(" + repr(arguments) + "); "
        "print(" + repr(RESULT_MARKER) + "+json.dumps({"
        "'rc':rc,'cli_file':cli.__file__,'loaded':[m for m in " + repr(HEAVY_STARTUP_MODULES) + " if m in sys.modules]}))"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(WORKTREE_SOURCE)
    environment["OPENAI_API_KEY"] = FAKE_OFFLINE_OPENAI_KEY
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-S", "-c", probe],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=HELP_COMPLETION_LIMIT_SECONDS,
        check=False,
    )
    elapsed = time.monotonic() - started
    marker_lines = [line for line in completed.stdout.splitlines() if line.startswith(RESULT_MARKER)]
    assert marker_lines, completed.stdout + completed.stderr
    return completed, elapsed, json.loads(marker_lines[-1][len(RESULT_MARKER):])


failures = []
for case_name, case_arguments in HELP_CASES:
    try:
        process, duration, result = run_isolated_help_probe(case_arguments)
        checks = {
            "exits 0": process.returncode == 0 and result["rc"] == 0,
            "prints usage/help": "spendguard" in process.stdout.lower(),
            "uses this worktree": pathlib.Path(result["cli_file"]).resolve().is_relative_to(WORKTREE_SOURCE.resolve()),
            "imports no heavy startup module": result["loaded"] == [],
            "finishes within named generous limit": duration < HELP_COMPLETION_LIMIT_SECONDS,
        }
    except Exception as error:
        checks = {f"probe completed ({type(error).__name__}: {error})": False}
    for description, passed in checks.items():
        label = f"{case_name} help {description}"
        print(("  [OK] " if passed else "  [FAIL] ") + label)
        if not passed:
            failures.append(label)

print(("\n[FAIL] " if failures else "\n[OK] ") +
      f"CLI help cheap/lazy guard: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
