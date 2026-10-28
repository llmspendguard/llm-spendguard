"""Prove convergence diagnostics cannot pollute JSON stdout."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import distribution  # noqa: E402

CONVERGENCE_DIAGNOSTIC = "fixture install-hook human diagnostic"


def test_distribute_json_convergence_routes_human_output_to_stderr():
    result = {"target": None, "check": False, "dry_run": False, "surfaces": [],
              "enumeration": {}, "summary": {"converged": 0, "total": 0, "stragglers": []},
              "exit_code": 0}

    def fixture_convergence_with_human_output(**_arguments):
        print(CONVERGENCE_DIAGNOSTIC)
        return result

    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.object(distribution, "distribute_fleet", side_effect=fixture_convergence_with_human_output), \
         contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = distribution.distribute_command(["--json"])

    assert exit_code == 0
    assert json.loads(stdout.getvalue()) == result
    assert CONVERGENCE_DIAGNOSTIC not in stdout.getvalue()
    assert CONVERGENCE_DIAGNOSTIC in stderr.getvalue()


if __name__ == "__main__":
    test_distribute_json_convergence_routes_human_output_to_stderr()
    print("PASS distribute --json convergence stdout is pure JSON and diagnostics remain on stderr")
