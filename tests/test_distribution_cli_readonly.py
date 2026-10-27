"""Exercise verify-only distribution through the actual module entry point."""
import json
import os
from pathlib import Path
import subprocess
import sysconfig
import tempfile
import venv

CLI_READONLY_SOURCE = Path(__file__).resolve().parents[1] / "src"
CLI_READONLY_ARGUMENTS = ("-B", "-m", "spendguard", "distribute", "--check", "--json")
CLI_READONLY_IMPORT_ARGUMENTS = ("-B", "-c", "import spendguard; import spendguard.config")


def exercise_distribution_cli_state_snapshot(state_exists):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        interpreter = root / "interpreter"
        venv.EnvBuilder(with_pip=False).create(interpreter)
        site = next(interpreter.glob("lib/python*/site-packages"))
        # Reuse installed dependencies, but fixture the host's startup hook.
        (site / "fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
        (site / "sitecustomize.py").write_text("# CLI-entry fixture: no ambient gate hook.\n")
        python = interpreter / "bin" / "python"
        state = home / "state"
        if state_exists:
            state.mkdir()
        original_entries = set(home.rglob("*"))
        environment = dict(os.environ, HOME=str(home), SPENDGUARD_HOME=str(state),
                           PYTHONPATH=str(CLI_READONLY_SOURCE), PYTHONDONTWRITEBYTECODE="1",
                           SPENDGUARD_NO_AUTOINSTALL="1", SPENDGUARD_TEST_ISOLATED="1",
                           SPENDGUARD_TEST_CLI_OFFLINE="1")
        result = subprocess.run([str(python), *CLI_READONLY_ARGUMENTS], env=environment,
                                capture_output=True, text=True, check=False)
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert result.stdout.strip(), result.stderr
        report = json.loads(result.stdout)
        assert report["check"] is True and report["exit_code"] == 1, report
        assert report["summary"]["stragglers"], report
        assert set(home.rglob("*")) == original_entries, list(home.rglob("*"))
        imported = subprocess.run([str(python), *CLI_READONLY_IMPORT_ARGUMENTS], env=environment,
                                  capture_output=True, text=True, check=False)
        assert imported.returncode == 0, (imported.stdout, imported.stderr)
        assert set(home.rglob("*")) == original_entries, list(home.rglob("*"))


def test_distribution_check_cli_entry_is_zero_mutation():
    exercise_distribution_cli_state_snapshot(state_exists=False)
    exercise_distribution_cli_state_snapshot(state_exists=True)


if __name__ == "__main__":
    test_distribution_check_cli_entry_is_zero_mutation()
    print("PASS actual distribution CLI entry leaves fresh HOME/state untouched")
