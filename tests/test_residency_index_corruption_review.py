"""Adversarial persisted membership corruption must rebuild from transcripts."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home("sg-index-corruption-")

from spendguard import claudecode, config

CORRUPTION_REVIEW_SESSION = "corruption-review-session"
CORRUPTION_REVIEW_TRANSCRIPT_NAME = "membership.jsonl"
CORRUPTION_REVIEW_STATE_FIELD = "sessions"
CORRUPTION_REVIEW_WORKERS = 4
CORRUPTION_REVIEW_LOOKUPS = 16


@pytest.mark.parametrize("broken_sessions", [[], ["incorrect-membership"], None])
def test_residency_corrupt_membership_rebuilds_complete_records(tmp_path, broken_sessions):
    transcript = tmp_path / CORRUPTION_REVIEW_TRANSCRIPT_NAME
    evidence = {"sessionId": CORRUPTION_REVIEW_SESSION, "type": "user",
                "message": {"role": "user", "content": "complete residency evidence"}}
    transcript.write_text(json.dumps(evidence) + "\n")
    with patch.object(claudecode, "_projects_dir", return_value=str(tmp_path)):
        assert claudecode._conv_transcript_records(CORRUPTION_REVIEW_SESSION) == [evidence]
        persisted = config.load_state(claudecode.RESIDENCY_TRANSCRIPT_INDEX_STATE)
        persisted["files"][str(transcript)][CORRUPTION_REVIEW_STATE_FIELD] = broken_sessions
        assert config.save_state(claudecode.RESIDENCY_TRANSCRIPT_INDEX_STATE, persisted)
        cold_records, _ = claudecode._scan_new_lines(transcript, 0)
        actual = claudecode._conv_transcript_records(CORRUPTION_REVIEW_SESSION)
        assert actual == cold_records, f"warm={actual!r}; cold={cold_records!r}"


@pytest.mark.parametrize("damage", ["truncated", "non-object", "old-version", "other-root", "missing-files"])
def test_residency_damaged_index_envelope_rebuilds(tmp_path, damage):
    transcript = tmp_path / CORRUPTION_REVIEW_TRANSCRIPT_NAME
    evidence = {"sessionId": CORRUPTION_REVIEW_SESSION}
    transcript.write_text(json.dumps(evidence) + "\n")
    with patch.object(claudecode, "_projects_dir", return_value=str(tmp_path)):
        assert claudecode._conv_transcript_records(CORRUPTION_REVIEW_SESSION) == [evidence]
        persisted = config.load_state(claudecode.RESIDENCY_TRANSCRIPT_INDEX_STATE)
        if damage == "truncated":
            payload = '{"version":'
        elif damage == "non-object":
            payload = "[]"
        else:
            if damage == "old-version":
                persisted["version"] = claudecode.RESIDENCY_TRANSCRIPT_INDEX_VERSION - 1
            elif damage == "other-root":
                persisted["root"] = str(tmp_path.parent)
            else:
                persisted.pop("files")
            payload = json.dumps(persisted)
        config.state_path(claudecode.RESIDENCY_TRANSCRIPT_INDEX_STATE).write_text(payload)
        assert claudecode._conv_transcript_records(CORRUPTION_REVIEW_SESSION) == [evidence]


def test_residency_concurrent_index_writers_preserve_membership(tmp_path):
    transcript = tmp_path / CORRUPTION_REVIEW_TRANSCRIPT_NAME
    evidence = {"sessionId": CORRUPTION_REVIEW_SESSION}
    transcript.write_text(json.dumps(evidence) + "\n")
    with patch.object(claudecode, "_projects_dir", return_value=str(tmp_path)):
        with ThreadPoolExecutor(max_workers=CORRUPTION_REVIEW_WORKERS) as executor:
            results = list(executor.map(claudecode._conv_transcript_records,
                           [CORRUPTION_REVIEW_SESSION] * CORRUPTION_REVIEW_LOOKUPS))
        assert all(records == [evidence] for records in results)
        assert claudecode._conv_transcript_records(CORRUPTION_REVIEW_SESSION) == [evidence]


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))
