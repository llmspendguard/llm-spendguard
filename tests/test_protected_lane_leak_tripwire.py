"""Offline script-style contract tests for the protected-lane leak tripwire."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

if os.environ.get("SPENDGUARD_TEST_ISOLATED") != "1":
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-protected-lane-")
    os.execv(sys.executable, [sys.executable, *sys.argv])
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-protected-lane-"))

from spendguard import cli  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRIPWIRE_PATH = REPOSITORY_ROOT / "scripts" / "protected_lane_leak_tripwire.py"
TEST_WINDOW_HOURS = "1"
PROTECTED_EXECUTOR = "claude-code"
PROTECTED_PROVIDER = "anthropic"
OTHER_PROVIDER = "openai"
OTHER_EXECUTOR = "codex"
TEST_TIMESTAMP = "2999-01-01T00:00:00Z"
TEST_COST = 1.25

tripwire_spec = importlib.util.spec_from_file_location("protected_lane_leak_tripwire_module", TRIPWIRE_PATH)
tripwire = importlib.util.module_from_spec(tripwire_spec)
tripwire_spec.loader.exec_module(tripwire)


def create_tripwire_ledger(path: Path, rows: list[tuple]) -> None:
    connection = sqlite3.connect(path)
    definitions = ", ".join(f"{column} TEXT" for column in tripwire.RECENT_CALL_READ_COLUMNS)
    connection.execute(f"CREATE TABLE calls ({definitions})")
    placeholders = ", ".join("?" for _ in tripwire.RECENT_CALL_READ_COLUMNS)
    for row in rows:
        connection.execute(f"INSERT INTO calls VALUES ({placeholders})", row)
    connection.commit()
    connection.close()


def tripwire_row(*, provider: str, executor, cost: float = 0.0) -> tuple:
    values = {column: None for column in tripwire.RECENT_CALL_READ_COLUMNS}
    values.update(id="call-id", ts=TEST_TIMESTAMP, intent="tripwire-test", provider=provider,
                  model="test-model", kind="subscription" if executor not in (None, "", "api") else "realtime",
                  in_tok="10", out_tok="2", cost=str(cost), executor=executor)
    return tuple(values[column] for column in tripwire.RECENT_CALL_READ_COLUMNS)


def invoke_tripwire(ledger_path: Path) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    arguments = ["--protect-executor", PROTECTED_EXECUTOR, "--protect-provider", PROTECTED_PROVIDER,
                 "--hours", TEST_WINDOW_HOURS, "--ledger-path", str(ledger_path)]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = tripwire.main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def invoke_lane_leak_check_cli(ledger_path: Path) -> tuple[int, str]:
    output = io.StringIO()
    arguments = ["--protect-executor", PROTECTED_EXECUTOR, "--protect-provider", PROTECTED_PROVIDER,
                 "--hours", TEST_WINDOW_HOURS, "--ledger-path", str(ledger_path)]
    with contextlib.redirect_stdout(output):
        exit_code = cli.lane_leak_check_cli(arguments)
    return exit_code, output.getvalue()


def run_tripwire_contract_tests() -> list[str]:
    failures = []
    cases = (
        ("CLEAN", [tripwire_row(provider=OTHER_PROVIDER, executor=OTHER_EXECUTOR)], 0, "CLEAN:"),
        ("LEAK metered", [tripwire_row(provider=PROTECTED_PROVIDER, executor=None, cost=TEST_COST)],
         1, "REAL DOLLARS"),
        ("LEAK plan", [tripwire_row(provider=PROTECTED_PROVIDER, executor=PROTECTED_EXECUTOR)],
         1, "$0 in ledger but consumes the capped plan"),
        ("INCONCLUSIVE", [], 2, "INCONCLUSIVE:"),
    )
    with tempfile.TemporaryDirectory(prefix="sg-tripwire-ledgers-") as directory:
        directory_path = Path(directory)
        for case_name, rows, expected_code, expected_text in cases:
            ledger_path = directory_path / f"{case_name.replace(' ', '-')}.sqlite"
            create_tripwire_ledger(ledger_path, rows)
            actual_code, stdout, stderr = invoke_tripwire(ledger_path)
            passed = actual_code == expected_code and expected_text in stdout and not stderr
            print(f"[{'OK' if passed else 'FAIL'}] {case_name}")
            if not passed:
                failures.append(f"{case_name}: code={actual_code}, stdout={stdout!r}, stderr={stderr!r}")

        absent_path = directory_path / "absent.sqlite"
        actual_code, stdout, stderr = invoke_tripwire(absent_path)
        passed = actual_code == 3 and not stdout and "PROBE FAILED:" in stderr
        print(f"[{'OK' if passed else 'FAIL'}] PROBE_FAILED")
        if not passed:
            failures.append(f"PROBE_FAILED: code={actual_code}, stdout={stdout!r}, stderr={stderr!r}")
    return failures


def test_lane_leak_check_cli_preserves_clean_leak_and_inconclusive_codes() -> None:
    cases = (
        ([tripwire_row(provider=OTHER_PROVIDER, executor=OTHER_EXECUTOR)], tripwire.CLEAN_EXIT, "CLEAN:"),
        ([tripwire_row(provider=PROTECTED_PROVIDER, executor=None, cost=TEST_COST)],
         tripwire.LEAK_EXIT, "REAL DOLLARS"),
        ([], tripwire.INCONCLUSIVE_EXIT, "INCONCLUSIVE:"),
    )
    with tempfile.TemporaryDirectory(prefix="sg-tripwire-cli-ledgers-") as directory:
        for case_number, (rows, expected_code, expected_text) in enumerate(cases):
            ledger_path = Path(directory) / f"case-{case_number}.sqlite"
            create_tripwire_ledger(ledger_path, rows)
            actual_code, output = invoke_lane_leak_check_cli(ledger_path)
            assert actual_code == expected_code
            assert expected_text in output


if __name__ == "__main__":
    test_failures = run_tripwire_contract_tests()
    try:
        test_lane_leak_check_cli_preserves_clean_leak_and_inconclusive_codes()
        print("[OK] CLI exit contract")
    except AssertionError as error:
        test_failures.append(f"CLI exit contract: {error}")
        print("[FAIL] CLI exit contract")
    print(f"{'FAIL' if test_failures else 'OK'}: protected-lane tripwire contract tests")
    for test_failure in test_failures:
        print(test_failure)
    sys.exit(1 if test_failures else 0)
