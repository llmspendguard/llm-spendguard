"""Unknown readiness evidence must stay unknown and keep the readiness exit red."""
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile

from spendguard import capability_readiness as readiness
from spendguard.capability_readiness_gates import (
    assess_capability_environment_evidence,
    assess_lane_queue_maintenance_readiness,
)

QUEUE_FIXTURE_SIZE_LIMIT = 1024 * 1024
QUEUE_FIXTURE_FREELIST_LIMIT = 1.0
QUEUE_FIXTURE_TERMINAL_LIMIT = 0
QUEUE_FIXTURE_RETAIN_DAYS = 7.0


def verify_capability_readiness_is_fail_closed():
    """Exercise unavailable evidence through the actual assessment and CLI core."""
    item = readiness.CapabilityReadinessItem(
        id='unreviewed-standard', description='Unreviewed external mapping', gate=None,
        arguments={}, reason='A reviewed deterministic gate has not been declared.',
    )
    with patch.object(readiness, 'load_capability_readiness_manifest', return_value=(1, (item,))):
        report = readiness.assess_capability_readiness()
        assert report['coverage'] == {'covered': 0, 'total': 1, 'passed': 0}
        assert report['items'][0]['status'] == 'UNREVIEWED'
        assert report['ready'] is False
        with redirect_stdout(StringIO()):
            assert readiness.run_capability_selfcheck() != readiness.READINESS_SUCCESS_EXIT
    evidence = assess_capability_environment_evidence(
        context={'environment_report': None}, patterns=(), required_checks=(),
    )
    assert evidence.status == 'UNREVIEWED'
    with patch.object(readiness, 'load_capability_readiness_manifest', side_effect=ValueError('bad manifest')):
        output = StringIO()
        with redirect_stdout(output):
            assert readiness.run_capability_selfcheck() != readiness.READINESS_SUCCESS_EXIT
        assert 'FAIL' in output.getvalue()


def test_environment_parity_manifest_and_report_outcomes(tmp_path):
    _, items = readiness.load_capability_readiness_manifest()
    environment_item = next(item for item in items if item.id == "env-parity-in-verification")
    required_checks = environment_item.arguments["required_checks"]
    assert {"catalog-portable", "keys-single-source"}.issubset(required_checks)
    log_path = tmp_path / "environment-check.log"
    log_path.write_text("complete clean-room check output\n")
    log_hash = hashlib.sha256(log_path.read_bytes()).hexdigest()
    successful_checks = {
        check_id: {"exit_code": 0, "log": str(log_path), "sha256": log_hash}
        for check_id in required_checks
    }
    report_path = tmp_path / "environment-report.json"
    report = {
        "fingerprint": "current-fingerprint",
        "environments": [
            {"interpreter": interpreter, "clean": True, "checks": successful_checks}
            for interpreter in ("python3.11", "python3.12")
        ],
    }
    report_path.write_text(json.dumps(report))
    arguments = dict(context={"repo_root": tmp_path, "environment_report": report_path},
                     patterns=environment_item.arguments["patterns"], required_checks=required_checks)
    with patch("spendguard.capability_readiness_gates.capability_verification_fingerprint",
               return_value="current-fingerprint"):
        assert assess_capability_environment_evidence(**arguments).status == "PASS"
        report["fingerprint"] = "stale-fingerprint"
        report_path.write_text(json.dumps(report))
        assert assess_capability_environment_evidence(**arguments).status == "UNREVIEWED"
        report["fingerprint"] = "current-fingerprint"
        report["environments"][0]["checks"][required_checks[0]]["exit_code"] = 1
        report_path.write_text(json.dumps(report))
        assert assess_capability_environment_evidence(**arguments).status == "FAIL"
    arguments["context"]["environment_report"] = None
    assert assess_capability_environment_evidence(**arguments).status == "UNREVIEWED"


def test_lane_queue_maintenance_readiness_fails_and_passes_on_fixtures(tmp_path):
    database_path = tmp_path / "lane_queue.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE lane_queue(state TEXT,terminal_ts TEXT,created_ts TEXT)")
        connection.execute("INSERT INTO lane_queue VALUES('failed','2000-01-01T00:00:00+00:00',"
                           "'2000-01-01T00:00:00+00:00')")
    arguments = dict(context={}, database_path=str(database_path), retain_days=QUEUE_FIXTURE_RETAIN_DAYS,
                     size_limit_bytes=QUEUE_FIXTURE_SIZE_LIMIT,
                     freelist_ratio_limit=QUEUE_FIXTURE_FREELIST_LIMIT,
                     old_terminal_limit=QUEUE_FIXTURE_TERMINAL_LIMIT)
    assert assess_lane_queue_maintenance_readiness(**arguments).status == "FAIL"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM lane_queue")
    assert assess_lane_queue_maintenance_readiness(**arguments).status == "PASS"


if __name__ == '__main__':
    verify_capability_readiness_is_fail_closed()
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        test_lane_queue_maintenance_readiness_fails_and_passes_on_fixtures(temporary_path)
        test_environment_parity_manifest_and_report_outcomes(temporary_path)
    print('readiness guard: unknown evidence and malformed manifests fail closed')
