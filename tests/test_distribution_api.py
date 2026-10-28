"""Offline API consumer attestation: red before repaired green, with check removal."""
import ast
import copy
import inspect
from unittest.mock import patch

from spendguard import distribution as distribution_api
from spendguard import fleet

API_FIXTURE_PID = 321
API_FIXTURE_CREATED = 123.0
API_FIXTURE_VERSION = "api-fixture-current"
API_FIXTURE_SHA = "api-fixture-current-sha"
API_FIXTURE_OLD = "api-fixture-old"
API_FIXTURE_PYTHON = "/fixture/api/python"
API_FIXTURE_OWNER = "fixture.api.owner"


def exercise_distribution_api_red_green():
    target = {"version": API_FIXTURE_VERSION, "sha": API_FIXTURE_SHA}
    clean = {"pid": API_FIXTURE_PID, "pid_create_time": API_FIXTURE_CREATED,
             "served_sha": API_FIXTURE_SHA, "version": API_FIXTURE_VERSION,
             "dirty": False, "gate_enforcing": True, "identity_verified": True, "liveness": fleet._FLEET_PID_VERIFIED_LIVE,
             "python_executable": API_FIXTURE_PYTHON, "supervision_ref": API_FIXTURE_OWNER}
    clean_probe = {"confirmed": True, "version": API_FIXTURE_VERSION, "metadata_version": API_FIXTURE_VERSION,
                   "sha": API_FIXTURE_SHA, "dirty": False, "gate_enforcing": True}
    state = {"consumer": copy.deepcopy(clean), "probe": copy.deepcopy(clean_probe),
             "identity": fleet._FLEET_PID_VERIFIED_LIVE}

    def repair_api_fixture_registration(**arguments):
        assert arguments == {"apply": True, "consumer_id": str(API_FIXTURE_PID), "all_supervised": True}
        state.update(consumer=copy.deepcopy(clean), probe=copy.deepcopy(clean_probe), identity=fleet._FLEET_PID_VERIFIED_LIVE)
        return {fleet.FLEET_RESTART_REQUESTED_KEY: [{"pid": API_FIXTURE_PID}]}

    mutations = (
        ("on_version", lambda: state["consumer"].update(served_sha=API_FIXTURE_OLD)),
        ("on_version", lambda: state["probe"].update(metadata_version=API_FIXTURE_OLD)),
        ("healthy", lambda: state.update(identity=fleet._FLEET_PID_UNVERIFIABLE)),
        ("healthy", lambda: state["consumer"].update(dirty=True)),
    )
    with patch.object(fleet, "running_consumers", side_effect=lambda **kwargs: [state["consumer"]]), \
         patch.object(fleet, "_classify_fleet_pid_identity", side_effect=lambda *args: state["identity"]), \
         patch.object(fleet, "restart_stale", side_effect=repair_api_fixture_registration) as restart, \
         patch.object(distribution_api, "probe_distribution_interpreter", side_effect=lambda *args: state["probe"]):
        for axis, mutate in mutations:
            mutate()
            red = distribution_api.attest_distribution_api_consumers(target, check=True, settings={})
            assert red[0]["surface"] == f"api:{API_FIXTURE_PID}" and red[0]["straggler_reason"]
            restart.assert_not_called()
            requested = distribution_api.attest_distribution_api_consumers(target, settings={})
            assert requested[0]["action_taken"] == "restart-requested"
            green = distribution_api.attest_distribution_api_consumers(target, check=True, settings={})
            assert not green[0]["straggler_reason"]
            restart.reset_mock()
            mutate()
            tree = ast.parse(inspect.getsource(distribution_api.attest_distribution_api_consumers))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(isinstance(name, ast.Name) and name.id == axis for name in node.targets):
                    node.value = ast.Constant(True)
            namespace = dict(vars(distribution_api))
            namespace["__name__"] = distribution_api.__name__
            exec(compile(ast.fix_missing_locations(tree), __file__, "exec"), namespace)
            mutant_rows = namespace["attest_distribution_api_consumers"](target, check=True, settings={})
            assert not mutant_rows[0]["straggler_reason"], "removed check must break the red assertion"
            repair_api_fixture_registration(apply=True, consumer_id=str(API_FIXTURE_PID), all_supervised=True)
        with patch.object(fleet, "restart_stale", return_value={}):
            assert distribution_api.attest_distribution_api_consumers(target, settings={})[0]["straggler_reason"]
        state["consumer"]["liveness"] = fleet._FLEET_PID_GONE
        assert distribution_api.attest_distribution_api_consumers(target, check=True, settings={}) == []
        state["consumer"] = {**clean, "cmdline": "spendguard mcp"}
        assert distribution_api.attest_distribution_api_consumers(target, check=True, settings={}) == []
    print("[OK] API frozen SHA, metadata, identity and dirty red→restart→green; removed checks break red; dead/MCP excluded")


if __name__ == "__main__":
    exercise_distribution_api_red_green()
