"""Unknown readiness evidence must stay unknown and keep the readiness exit red."""
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from spendguard import capability_readiness as readiness
from spendguard.capability_readiness_gates import assess_capability_environment_evidence


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


if __name__ == '__main__':
    verify_capability_readiness_is_fail_closed()
    print('readiness guard: unknown evidence and malformed manifests fail closed')

