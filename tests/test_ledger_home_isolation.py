"""Reproduce late isolation after gated startup, without accessing any user home.

The child starts with a disposable original home, just as sitecustomize starts
with the user's home. It then changes only the environment, as the leaking
fixture did. Every attempted write must refuse before changing original bytes.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

LEDGER_ISOLATION_CHILD_TIMEOUT_S = 60
LEDGER_ISOLATION_WRITER_CASES = ("calls", "ledger", "explicit", "pooled")
LEDGER_ISOLATION_CHILD_SOURCE = r'''
import os
import pathlib
import sys
from spendguard import calls, config
from spendguard.ledger import SpendLedger

original = pathlib.Path(os.environ["SPENDGUARD_HOME"])
isolated = pathlib.Path(sys.argv[1])
writer = sys.argv[2]
assert config.HOME == original, "startup must cache the original disposable home"
# Substitute only the protected default's identity; all actual paths stay temporary.
config.DEFAULT_SPENDGUARD_HOME = original
cached_path = config.db_path()
pool_key = __name__
zero_cost = 0
event = {"source": __name__, "model": __name__, "cost_basis": "unpriced"}
if writer == "pooled":
    config.pooled_ledger_conn(pool_key, lambda connection: None)

# Include WAL and all auxiliary files: refusal may not even initialize a schema.
before = {p.relative_to(original): p.read_bytes()
          for p in original.rglob("*") if p.is_file()}
os.environ["SPENDGUARD_HOME"] = str(isolated)
refused = False
try:
    if writer == "calls":
        calls.record_call(__name__, __name__, "realtime", zero_cost)
    elif writer == "pooled":
        config.pooled_ledger_conn(pool_key, lambda connection: None)
    else:
        ledger = SpendLedger(cached_path if writer == "explicit" else None)
        try:
            ledger.record_event(event)
        finally:
            ledger._conn.close()
except BaseException as error:
    assert type(error).__name__ == "LedgerHomeIsolationError", repr(error)
    refused = True
assert refused, f"{writer} silently used the startup home after isolation changed"
after = {p.relative_to(original): p.read_bytes()
         for p in original.rglob("*") if p.is_file()}
assert before == after, "refusal must preserve all original ledger bytes"
'''


@pytest.mark.parametrize("writer_case", LEDGER_ISOLATION_WRITER_CASES)
def test_late_home_change_refuses_startup_ledger(writer_case):
    """Fault: startup cached a different home; include an already-pooled handle."""
    with tempfile.TemporaryDirectory(prefix=__name__) as original_home:
        with tempfile.TemporaryDirectory(prefix=__name__) as isolated_home:
            child_env = dict(os.environ)
            child_env["SPENDGUARD_HOME"] = original_home
            child_env["SPENDGUARD_CALLS"] = "1"
            result = subprocess.run(
                [sys.executable, "-c", LEDGER_ISOLATION_CHILD_SOURCE, isolated_home, writer_case],
                env=child_env, capture_output=True, text=True,
                timeout=LEDGER_ISOLATION_CHILD_TIMEOUT_S,
            )
            assert result.returncode == 0, (writer_case, result.stdout, result.stderr)


def test_aligned_home_keeps_fixture_events_isolated(tmp_path):
    """Containment must still allow the normal explicitly isolated writer."""
    from spendguard.ledger import SpendLedger

    fixture_path = tmp_path / pathlib.Path(__file__).stem
    fixture_ledger = SpendLedger(fixture_path)
    try:
        fixture_event_id = fixture_ledger.record_event(
            {"source": __name__, "cost_basis": "unpriced"})
        assert fixture_ledger.get(fixture_event_id)["source"] == __name__
    finally:
        fixture_ledger._conn.close()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
