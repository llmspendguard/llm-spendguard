"""Readiness artifact transport preserves complete logs and rejects altered evidence."""
import hashlib
import json
from pathlib import Path
import runpy
import tempfile

TRANSPORT_HELPER_RELATIVE_PATH = Path("scripts/test/readiness_environment_evidence.py")
TRANSPORT_LOG_FIXTURE = "first line\ncomplete evidence tail\n"
TRANSPORT_ALTERED_LOG_FIXTURE = "first line\naltered evidence tail\n"
TRANSPORT_TEST_FINGERPRINT = "source-input-fingerprint"
TRANSPORT_OTHER_FINGERPRINT = "different-source-input-fingerprint"
TRANSPORT_REPORT_FIXTURE_NAME = "environment.json"
TRANSPORT_LOG_FIXTURE_NAME = "verification.log"


def verify_readiness_transport_rejects_changed_or_mismatched_evidence():
    repository_root = Path(__file__).resolve().parents[1]
    transport_module = runpy.run_path(str(repository_root / TRANSPORT_HELPER_RELATIVE_PATH))
    merge_records = transport_module["merge_readiness_environment_records"]
    with tempfile.TemporaryDirectory() as temporary_directory:
        artifact_root = Path(temporary_directory)
        source_artifact = artifact_root / "source"
        source_artifact.mkdir()
        log_path = source_artifact / TRANSPORT_LOG_FIXTURE_NAME
        log_path.write_text(TRANSPORT_LOG_FIXTURE)
        report = {"fingerprint": TRANSPORT_TEST_FINGERPRINT, "environments": [{
            "interpreter": "fixture-interpreter", "clean": True, "checks": {"fixture-check": {
                "log": log_path.name, "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                "exit_code": 0,
            }},
        }]}
        report_path = source_artifact / TRANSPORT_REPORT_FIXTURE_NAME
        report_path.write_text(json.dumps(report))
        merged_path = artifact_root / "merged.json"
        merge_records(source_artifact, merged_path)
        merged = json.loads(merged_path.read_text())
        merged_check = merged["environments"][0]["checks"]["fixture-check"]
        assert Path(merged_check["log"]).read_text() == TRANSPORT_LOG_FIXTURE
        assert merged_check["sha256"] == report["environments"][0]["checks"]["fixture-check"]["sha256"]
        log_path.write_text(TRANSPORT_ALTERED_LOG_FIXTURE)
        try:
            merge_records(source_artifact, merged_path)
        except ValueError as transport_error:
            assert "changed" in str(transport_error)
        else:
            raise AssertionError("Changed complete evidence was accepted")
        log_path.write_text(TRANSPORT_LOG_FIXTURE)
        other_artifact = artifact_root / "other"
        other_artifact.mkdir()
        report["fingerprint"] = TRANSPORT_OTHER_FINGERPRINT
        (other_artifact / TRANSPORT_REPORT_FIXTURE_NAME).write_text(json.dumps(report))
        (other_artifact / TRANSPORT_LOG_FIXTURE_NAME).write_text(TRANSPORT_LOG_FIXTURE)
        try:
            merge_records(artifact_root, artifact_root / "result.json")
        except ValueError as transport_error:
            assert "different source inputs" in str(transport_error)
        else:
            raise AssertionError("Mismatched source fingerprints were accepted")


if __name__ == "__main__":
    verify_readiness_transport_rejects_changed_or_mismatched_evidence()
    print("readiness transport: complete logs preserved; tampering and mismatched inputs rejected")
