"""Whole-command parity against the pre-index scan after directory mutations."""
from unittest.mock import patch

from test_residency_transcript_index_work import (
    INDEX_WORK_CACHED_TOKENS, INDEX_WORK_COST, INDEX_WORK_OUTPUT_TOKENS,
    INDEX_WORK_SESSION, residency_index_capture_command,
    residency_index_fixture_record, residency_index_legacy_records,
    residency_index_seed_corpus, residency_index_write_records,
)
from _pytest_fixture_bridge import run_pytest_style
from spendguard import budget, calls, claudecode, setup

MUTATION_PARITY_NEW_FILENAME = "new-session-evidence.jsonl"
MUTATION_PARITY_RENAMED_FILENAME = "renamed-session-evidence.jsonl"
MUTATION_PARITY_MESSAGE_ID = "directory-mutation-evidence"
MUTATION_PARITY_DEDUP_KEY = "directory-mutation-charge"


def test_residency_membership_directory_mutations_command_parity(tmp_path):
    transcript_root = tmp_path / "transcripts"
    model = residency_index_seed_corpus(transcript_root)
    budget._ledger()
    calls._calls_db()
    budget._record_spend_event("anthropic", model, "est_chat", INDEX_WORK_COST,
        source="claude-code", conv_id=INDEX_WORK_SESSION,
        in_tok=1, out_tok=INDEX_WORK_OUTPUT_TOKENS,
        cache_read_tok=INDEX_WORK_CACHED_TOKENS, dedup_key=MUTATION_PARITY_DEDUP_KEY)
    new_path = transcript_root / MUTATION_PARITY_NEW_FILENAME
    renamed_path = transcript_root / MUTATION_PARITY_RENAMED_FILENAME
    with patch.object(claudecode, "_projects_dir", return_value=str(transcript_root)), \
            patch.object(claudecode, "_resident_instruction_tokens", return_value=(0, [])), \
            patch.object(setup, "_startup_gate_active", return_value=True), \
            patch("spendguard.delegate_router.delegation_doctor_line", return_value="mutation fixture lanes"), \
            patch("spendguard.lane_value.refresh_lane_value_if_stale"):
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        for mutation in ("add", "rename", "delete"):
            if mutation == "add":
                residency_index_write_records(new_path, [residency_index_fixture_record(
                    INDEX_WORK_SESSION, MUTATION_PARITY_MESSAGE_ID, model,
                    [{"type": "text", "text": "whole new-file evidence"}])])
            elif mutation == "rename":
                new_path.rename(renamed_path)
            else:
                renamed_path.unlink()
            expected_records = residency_index_legacy_records(INDEX_WORK_SESSION)
            assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == expected_records, mutation
            for command in ("doctor", "receipt"):
                with patch.object(claudecode, "_conv_transcript_records", residency_index_legacy_records):
                    expected = residency_index_capture_command(command)
                assert residency_index_capture_command(command) == expected, (mutation, command)


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))
