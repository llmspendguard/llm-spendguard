"""Transcript membership indexing preserves whole-command evidence with bounded warm parsing."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home("sg-residency-index-")

import spendguard
from spendguard import budget, calls, claudecode, config, gate, receipt, setup

INDEX_WORK_SESSION = "index-work-session"
INDEX_WORK_UNRELATED_FILES = 17
INDEX_WORK_UNRELATED_ROWS = 9
INDEX_WORK_LEDGER_ROWS = 23
INDEX_WORK_SELECTED_FILES = 2
INDEX_WORK_CACHED_TOKENS = 1200
INDEX_WORK_OUTPUT_TOKENS = 11
INDEX_WORK_COST = 0.25
INDEX_WORK_STATE_NAME = "residency_transcript_index"


def residency_index_fixture_record(session, message_id, model, content):
    return {"sessionId": session, "type": "assistant", "message": {
        "id": message_id, "role": "assistant", "model": model,
        "usage": {"input_tokens": 1, "output_tokens": INDEX_WORK_OUTPUT_TOKENS,
                  "cache_read_input_tokens": INDEX_WORK_CACHED_TOKENS}, "content": content}}


def residency_index_write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def residency_index_legacy_records(conv_id):
    """Exact pre-B5 scan semantics, intentionally independent of the new index."""
    records, seen = [], set()
    for path in sorted(Path(claudecode._projects_dir()).rglob("*.jsonl")):
        rows, _ = claudecode._scan_new_lines(path, 0)
        for row in rows:
            if row.get("sessionId") != conv_id:
                continue
            message_id = (row.get("message") or {}).get("id")
            if message_id is not None:
                if message_id in seen:
                    continue
                seen.add(message_id)
            records.append(row)
    return records


def residency_index_seed_corpus(root):
    model = config.advisor_model()
    first = residency_index_fixture_record(INDEX_WORK_SESSION, "index-first", model,
        [{"type": "tool_use", "id": "index-tool", "name": "Read"}])
    result = {"sessionId": INDEX_WORK_SESSION, "type": "user", "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "index-tool",
                                     "content": "complete resident tool evidence"}]}}
    last = residency_index_fixture_record(INDEX_WORK_SESSION, "index-last", model,
                                         [{"type": "text", "text": "final evidence"}])
    # File names deliberately do not encode sessionId; resume duplicates retain legacy order.
    residency_index_write_records(root / "project" / "a-original.jsonl", [first, result])
    residency_index_write_records(root / "branch" / "z-resumed.jsonl", [first, result, last])
    for index in range(INDEX_WORK_UNRELATED_FILES):
        unrelated = [residency_index_fixture_record(f"other-{index}", f"other-{index}-{row}",
                     model, [{"type": "text", "text": "unrelated evidence"}])
                     for row in range(INDEX_WORK_UNRELATED_ROWS)]
        residency_index_write_records(root / "unrelated" / f"other-{index}.jsonl", unrelated)
    return model


def residency_index_capture_command(command):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = gate._cli("doctor") if command == "doctor" else receipt.cli(["--all"])
    return status, output.getvalue()


def test_residency_index_bounds_warm_full_commands(tmp_path):
    spendguard.require()
    transcript_root = tmp_path / "transcripts"
    model = residency_index_seed_corpus(transcript_root)
    budget._ledger()
    calls._calls_db()
    for ledger_index in range(INDEX_WORK_LEDGER_ROWS):
        budget._record_spend_event("anthropic", model, "est_chat", INDEX_WORK_COST,
            source="claude-code", conv_id=INDEX_WORK_SESSION,
            in_tok=1, out_tok=INDEX_WORK_OUTPUT_TOKENS,
            cache_read_tok=INDEX_WORK_CACHED_TOKENS, dedup_key=f"index-work-charge-{ledger_index}")
    with patch.object(claudecode, "_projects_dir", return_value=str(transcript_root)), \
            patch.object(claudecode, "_resident_instruction_tokens", return_value=(0, [])), \
            patch.object(setup, "_startup_gate_active", return_value=True), \
            patch("spendguard.delegate_router.delegation_doctor_line", return_value="index fixture lanes"), \
            patch("spendguard.lane_value.refresh_lane_value_if_stale"):
        # Warm the index using the same complete target lookup both commands consume.
        expected_records = residency_index_legacy_records(INDEX_WORK_SESSION)
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == expected_records
        observed = {}
        for command in ("doctor", "receipt"):
            with patch.object(claudecode, "_conv_transcript_records", residency_index_legacy_records):
                expected_status, expected_output = residency_index_capture_command(command)
            with patch.object(claudecode, "_scan_new_lines", wraps=claudecode._scan_new_lines) as scan:
                actual_status, actual_output = residency_index_capture_command(command)
            assert actual_status == expected_status
            assert actual_output == expected_output, command
            assert "residency : Est sub value cache-read" in actual_output, actual_output
            observed[command] = scan.call_count
        print(f"warm full-command transcript scans: {observed}; selected files={INDEX_WORK_SELECTED_FILES}")
        assert all(count <= INDEX_WORK_SELECTED_FILES for count in observed.values()), observed


def test_residency_index_refresh_preserves_complete_records(tmp_path):
    transcript_root = tmp_path / "transcripts"
    model = residency_index_seed_corpus(transcript_root)
    with patch.object(claudecode, "_projects_dir", return_value=str(transcript_root)):
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        unrelated_path = transcript_root / "unrelated" / "other-0.jsonl"
        with unrelated_path.open("a") as appended:
            appended.write(json.dumps(residency_index_fixture_record(INDEX_WORK_SESSION, "late-appended",
                model, [{"type": "text", "text": "late evidence from previously unrelated file"}])) + "\n")
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        replacement_path = transcript_root / "replacement.jsonl"
        residency_index_write_records(replacement_path, [residency_index_fixture_record(INDEX_WORK_SESSION,
            "replacement-evidence", model, [{"type": "text", "text": "replacement evidence"}])])
        os.replace(replacement_path, unrelated_path)
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        rewrite_stat = unrelated_path.stat()
        rewrite_records = json.loads(unrelated_path.read_text())
        rewrite_records["message"]["content"][0]["text"] = "replacement EVIDENCE"
        residency_index_write_records(unrelated_path, [rewrite_records])
        assert unrelated_path.stat().st_size == rewrite_stat.st_size
        os.utime(unrelated_path, ns=(rewrite_stat.st_atime_ns, rewrite_stat.st_mtime_ns))
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        renamed_path = transcript_root / "renamed.jsonl"
        unrelated_path.rename(renamed_path)
        (transcript_root / "project" / "a-original.jsonl").unlink()
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        # A corrupt persisted cache cannot remove evidence or count as a valid empty index.
        config.save_state(INDEX_WORK_STATE_NAME, {"files": "invalid cache structure"})
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
        with patch.object(claudecode, "_scan_new_lines", wraps=claudecode._scan_new_lines) as scan:
            assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)
            # Legacy oracle is independently counted outside the optimized operation below.
            oracle_scans = len(list(transcript_root.rglob("*.jsonl")))
            optimized_scans = scan.call_count - oracle_scans
        assert optimized_scans <= INDEX_WORK_SELECTED_FILES, optimized_scans


def test_residency_index_refuses_unreadable_and_racing_files(tmp_path):
    transcript_root = tmp_path / "transcripts"
    residency_index_seed_corpus(transcript_root)
    with patch.object(claudecode, "_projects_dir", return_value=str(transcript_root)):
        claudecode._conv_transcript_records(INDEX_WORK_SESSION)
        original_scan = claudecode._scan_new_lines
        with patch.object(claudecode, "_scan_new_lines", side_effect=PermissionError("unreadable transcript")):
            with pytest.raises(PermissionError, match="unreadable transcript"):
                claudecode._conv_transcript_records(INDEX_WORK_SESSION)

        def residency_index_racing_scan(path, from_line, **kwargs):
            scanned = original_scan(path, from_line, **kwargs)
            with Path(path).open("a") as raced_file:
                raced_file.write("\n")
            return scanned

        with patch.object(claudecode, "_scan_new_lines", side_effect=residency_index_racing_scan):
            with pytest.raises(OSError, match="transcript changed during residency read"):
                claudecode._conv_transcript_records(INDEX_WORK_SESSION)
        assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == residency_index_legacy_records(INDEX_WORK_SESSION)


def test_residency_scan_never_swallows_memory_failure(tmp_path):
    transcript = tmp_path / "memory-failure.jsonl"
    residency_index_write_records(transcript, [{"sessionId": INDEX_WORK_SESSION}])
    for resource_failure in (MemoryError, RecursionError):
        with patch.object(claudecode.json, "loads", side_effect=resource_failure("complete evidence unavailable")):
            with pytest.raises(resource_failure, match="complete evidence unavailable"):
                claudecode._scan_new_lines(transcript, 0, strict=True)
    valid_record = {"sessionId": INDEX_WORK_SESSION}
    transcript.write_text(json.dumps(valid_record) + "\n{malformed json\n" + json.dumps(valid_record) + "\n")
    expected_records, expected_lines = claudecode._scan_new_lines(transcript, 0)
    strict_records, strict_lines = claudecode._scan_new_lines(transcript, 0, strict=True)
    assert strict_records == expected_records == [valid_record, valid_record]
    assert strict_lines == expected_lines == len(transcript.read_text().splitlines())


def test_residency_inflight_tail_completion_restores_membership(tmp_path):
    transcript_root = tmp_path / "transcripts"
    transcript = transcript_root / "inflight.jsonl"
    unrelated = {"sessionId": "inflight-other-session"}
    completed = {"sessionId": INDEX_WORK_SESSION, "message": {"id": "inflight-completed"}}
    residency_index_write_records(transcript, [unrelated])
    completed_line = json.dumps(completed) + "\n"
    # Pause a real writer midway through its final JSON object, then build the
    # index while that target session is still absent from all complete records.
    split_at = completed_line.index(":")
    with transcript.open("a") as writer:
        writer.write(completed_line[:split_at])
        writer.flush()
        with patch.object(claudecode, "_projects_dir", return_value=str(transcript_root)):
            assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == []
            writer.write(completed_line[split_at:])
            writer.flush()
            expected = residency_index_legacy_records(INDEX_WORK_SESSION)
            assert expected == [completed]
            assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == expected
            assert claudecode._conv_transcript_records(INDEX_WORK_SESSION) == expected


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))
