"""Doctor remains bounded when overage and residency caches are stale."""
import atexit
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


_DOCTOR_WALL_BUDGET_SECONDS = 2.0
_SEEDED_LEDGER_ROWS = 50_000
_STALE_CACHE_TIMESTAMP = 1.0
_TRANSCRIPT_FILE_COUNT = 12
_TRANSCRIPT_RECORD_COUNT = 2_000
_TRANSCRIPT_TEXT_BYTES = 4_096
_TRANSCRIPT_SESSION_ID = "doctor-bounded-residency-session"
_TRANSCRIPT_MODEL = "claude-sonnet-4-6"
_EXPECTED_DIAGNOSTIC_EXIT_CODE = 1
_SUBPROCESS_DOCTOR_PROBE = (
    "from unittest.mock import patch\n"
    "from spendguard import gate, setup\n"
    "with patch.object(setup, '_startup_gate_active', return_value=False):\n"
    "    raise SystemExit(gate._cli('doctor'))\n"
)
_AMBIENT_PROVIDER_ENVIRONMENT_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "SPENDGUARD_ENV",
    "SPENDGUARD_KEYS_ENV",
    "VOYAGE_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_API_KEY",
)

isolated_home_owner = tempfile.TemporaryDirectory(prefix="spendguard-doctor-bounded-")
atexit.register(isolated_home_owner.cleanup)
isolated_home = isolated_home_owner.name
ledger_path = os.path.join(isolated_home, "spend.db")
database = sqlite3.connect(ledger_path)
database.execute("CREATE TABLE spend_events(id TEXT PRIMARY KEY, occurred_at TEXT, source TEXT, cost_usd REAL)")
database.executemany(
    "INSERT INTO spend_events VALUES(?,?,?,?)",
    ((str(row), "2026-10-01T00:00:00+00:00", "fixture", 0.0)
     for row in range(_SEEDED_LEDGER_ROWS)),
)
database.commit()
database.close()

transcript_root = os.path.join(isolated_home, "transcripts")
os.makedirs(transcript_root)
transcript_record = json.dumps({
    "sessionId": _TRANSCRIPT_SESSION_ID,
    "type": "assistant",
    "message": {"model": _TRANSCRIPT_MODEL, "usage": {"input_tokens": 1},
                "content": [{"type": "text", "text": "x" * _TRANSCRIPT_TEXT_BYTES}]},
}) + "\n"
for transcript_index in range(_TRANSCRIPT_FILE_COUNT):
    with open(os.path.join(transcript_root, f"fixture-{transcript_index}.jsonl"), "w", encoding="utf-8") as transcript:
        transcript.writelines(transcript_record for _record_index in range(_TRANSCRIPT_RECORD_COUNT))

with open(os.path.join(isolated_home, "overage_status_cache.json"), "w", encoding="utf-8") as cache_file:
    json.dump({"ts": _STALE_CACHE_TIMESTAMP, "status": {"on_overage_now": False}}, cache_file)

for environment_variable in _AMBIENT_PROVIDER_ENVIRONMENT_VARIABLES:
    os.environ.pop(environment_variable, None)

environment = dict(os.environ)
environment.update({"SPENDGUARD_HOME": isolated_home, "SPENDGUARD_NO_AUTOINSTALL": "1",
                    "SPENDGUARD_CC_DIR": transcript_root})
environment.pop("SPENDGUARD_TEST_ISOLATED", None)
source_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
environment["PYTHONPATH"] = source_root

os.environ.update({"SPENDGUARD_HOME": isolated_home, "SPENDGUARD_NO_AUTOINSTALL": "1",
                   "SPENDGUARD_CC_DIR": transcript_root})
os.environ.pop("SPENDGUARD_TEST_ISOLATED", None)
sys.path.insert(0, source_root)
from spendguard import claudecode, gate, overage, setup  # noqa: E402


def fail_if_live_overage_refresh_runs():
    raise AssertionError("ordinary status read ran the unbounded transcript/ledger refresh")


overage._compute_overage_status = fail_if_live_overage_refresh_runs
status_started = time.monotonic()
cached_status = overage.current_overage_status()
status_elapsed = time.monotonic() - status_started


def fail_if_live_residency_decomposition_runs(*_args, **_kwargs):
    raise AssertionError("doctor ran live transcript residency decomposition")


doctor_probe_output = io.StringIO()
with patch.object(claudecode, "residency_decomposition",
                  side_effect=fail_if_live_residency_decomposition_runs), \
        patch.object(claudecode, "_conv_transcript_records",
                     side_effect=fail_if_live_residency_decomposition_runs), \
        patch.object(setup, "_startup_gate_active", return_value=False), \
        contextlib.redirect_stdout(doctor_probe_output):
    bounded_doctor_returncode = gate._cli("doctor")

started = time.monotonic()
completed = subprocess.run(
    [sys.executable, "-S", "-c", _SUBPROCESS_DOCTOR_PROBE], env=environment,
    capture_output=True, text=True, check=False, timeout=_DOCTOR_WALL_BUDGET_SECONDS,
)
elapsed = time.monotonic() - started
combined_output = completed.stdout + completed.stderr

failures = []
if status_elapsed >= overage.OVERAGE_DIAGNOSTIC_WALL_BUDGET_SECONDS:
    failures.append(f"current_overage_status exceeded wall budget: {status_elapsed:.3f}s")
if cached_status.get("on_overage_now") is not None or not cached_status.get("stale"):
    failures.append("current_overage_status did not return an explicit stale/unknown verdict")
if elapsed >= _DOCTOR_WALL_BUDGET_SECONDS:
    failures.append(f"doctor exceeded wall budget: {elapsed:.3f}s")
if bounded_doctor_returncode != _EXPECTED_DIAGNOSTIC_EXIT_CODE:
    failures.append(f"in-process fatal-seam doctor failed with {bounded_doctor_returncode}")
if "residency : unknown (stale" not in doctor_probe_output.getvalue():
    failures.append("doctor did not disclose the absent residency cache as stale/unknown")
if "overage UNKNOWN" not in combined_output:
    failures.append("doctor did not disclose the stale cached verdict as UNKNOWN")
if "residency : unknown (stale" not in combined_output:
    failures.append("subprocess doctor did not disclose the absent residency cache as stale/unknown")
if completed.returncode != _EXPECTED_DIAGNOSTIC_EXIT_CODE:
    failures.append(f"doctor failed with {completed.returncode}: {combined_output}")

print(f"{'[FAIL]' if failures else 'OK'} test_doctor_bounded_overage_status: "
      f"{len(failures)} failure(s); doctor wall {elapsed:.3f}s")
for failure in failures:
    print("  " + failure)
sys.exit(1 if failures else 0)
