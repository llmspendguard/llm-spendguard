"""Unknown readiness evidence must stay unknown and keep the readiness exit red."""
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path
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
        test_lane_queue_maintenance_readiness_fails_and_passes_on_fixtures(Path(temporary_directory))
    print('readiness guard: unknown evidence and malformed manifests fail closed')
