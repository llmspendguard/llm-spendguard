"""A live importer cannot borrow governance from a fresh interpreter."""
import contextlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from spendguard import distribution, fleet

RUNNING_API_TARGET = {"version": "running-api-fixture", "sha": "running-api-fixture-sha"}
RUNNING_API_TIMEOUT_S = distribution.DISTRIBUTION_PROBE_TIMEOUT_S
RUNNING_API_CHILD = '''import json,sys
from unittest.mock import patch
from spendguard import fleet,release,_version
target=json.loads(sys.argv[1])
with patch.object(release,'served_sha',return_value=dict(target,dirty=False)), patch.object(_version,'__version__',target['version']):
 fleet.register_consumer()
print(json.dumps(dict(pid=__import__('os').getpid())),flush=True)
sys.stdin.readline()
'''


def test_distribution_running_api_disabled_is_straggler():
    with tempfile.TemporaryDirectory() as temporary:
        environment = dict(os.environ, HOME=temporary, SPENDGUARD_HOME=temporary,
                           GATE_DISABLE="1", SPENDGUARD_NO_AUTOINSTALL="1",
                           PYTHONPATH=str(Path(fleet.__file__).resolve().parents[1]))
        with subprocess.Popen([sys.executable, "-c", RUNNING_API_CHILD, json.dumps(RUNNING_API_TARGET)],
                              env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            try:
                pid = json.loads(child.stdout.readline())["pid"]
                fresh = dict(RUNNING_API_TARGET, confirmed=True, gate_enforcing=True, dirty=False,
                             metadata_version=RUNNING_API_TARGET["version"])
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(fleet.config, "HOME", Path(temporary)))
                    stack.enter_context(patch.object(distribution, "resolve_distribution_target", return_value=RUNNING_API_TARGET))
                    stack.enter_context(patch.object(distribution.config, "consumer_audit_settings", return_value={}))
                    stack.enter_context(patch.object(distribution.consumer_audit, "audit_consumer_workspaces",
                                                    return_value={"consumers": [], "pruned_at_depth": []}))
                    stack.enter_context(patch.object(distribution.setup, "discover_distribution_interpreters",
                                                    return_value={"interpreters": [], "gaps": []}))
                    stack.enter_context(patch.object(distribution, "attest_distribution_daemons", return_value=[]))
                    stack.enter_context(patch.object(distribution, "attest_distribution_host",
                                                    return_value=distribution.distribution_surface_row("host", "host", True, True)))
                    stack.enter_context(patch.object(distribution, "probe_distribution_interpreter", return_value=fresh))
                    result = distribution.distribute_fleet(check=True)
                    assert result["exit_code"] != 0, result
                    assert result["summary"]["stragglers"] == [f"api:{pid}"], result
                    row = result["surfaces"][0]
                    assert row["on_version"] and not row["healthy"], row
                    assert row["registration"].get("gate_enforcing") is False, row
            finally:
                child.communicate(input="\n", timeout=RUNNING_API_TIMEOUT_S)


def test_distribution_running_api_guard_removal_is_red():
    original = distribution.attest_distribution_api_consumers
    guard = ' and candidate.get("gate_enforcing") is True'
    source = inspect.getsource(original)
    assert source.count(guard) == 1
    namespace = dict(vars(distribution))
    exec(compile(source.replace(guard, ""), inspect.getfile(original), "exec"), namespace)
    with patch.object(distribution, original.__name__, namespace[original.__name__]):
        try:
            test_distribution_running_api_disabled_is_straggler()
        except AssertionError:
            print("MUTATION RED: live disabled importer becomes false-green without own enforcement guard")
        else:
            raise AssertionError("running importer enforcement mutant survived")


if __name__ == "__main__":
    test_distribution_running_api_disabled_is_straggler()
    test_distribution_running_api_guard_removal_is_red()
    print("PASS live disabled importer remains named straggler despite enforcing fresh probe")
