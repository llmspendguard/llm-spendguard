"""Each capstone guard rejects its own fixture, and its removal kills that assertion."""
import contextlib
import copy
import inspect
from unittest.mock import patch

from spendguard import distribution as guard_api
from spendguard import distribution_daemon as guard_daemon

GUARD_REMOVAL_PID = 693
GUARD_REMOVAL_CREATED = 263.0
GUARD_REMOVAL_VERSION = "guard-removal-current"
GUARD_REMOVAL_SHA = "guard-removal-green"
GUARD_REMOVAL_PYTHON = "/fixture/guard-removal/python"
GUARD_REMOVAL_OWNER = "fixture.guard-removal.owner"
GUARD_REMOVAL_ENDPOINT = "http://guard-removal.invalid/mcp"
GUARD_REMOVAL_LEDGER = "/fixture/guard-removal/ledger.db"
GUARD_REMOVAL_TIMEOUT = 1.0


def compile_distribution_guard_removal(function, replacements):
    """Compile the entire production function with only the named guard removed."""
    source = inspect.getsource(function)
    for original, replacement in replacements:
        assert original in source, (function.__name__, original)
        source = source.replace(original, replacement)
    namespace = dict(function.__globals__)
    exec(compile(source, inspect.getfile(function), "exec"), namespace)
    return namespace[function.__name__]


def require_distribution_guard_mutant_killed(assertion, owner, symbol, mutant):
    assertion()
    with patch.object(owner, symbol, mutant):
        try:
            assertion()
        except AssertionError:
            pass
        else:
            raise AssertionError(f"removed {symbol} guard survived its rejection assertion")
    assertion()


@contextlib.contextmanager
def distribution_api_guard_fixture():
    target = {"version": GUARD_REMOVAL_VERSION, "sha": GUARD_REMOVAL_SHA}
    registration = {
        "pid": GUARD_REMOVAL_PID, "pid_create_time": GUARD_REMOVAL_CREATED,
        "served_sha": GUARD_REMOVAL_SHA, "version": GUARD_REMOVAL_VERSION,
        "dirty": False, "identity_verified": True,
        "liveness": guard_api.fleet._FLEET_PID_VERIFIED_LIVE,
        "python_executable": GUARD_REMOVAL_PYTHON, "supervision_ref": GUARD_REMOVAL_OWNER,
        "gate_enforcing": True,
    }
    probe = {"confirmed": True, "version": GUARD_REMOVAL_VERSION,
             "metadata_version": GUARD_REMOVAL_VERSION, "sha": GUARD_REMOVAL_SHA,
             "dirty": False, "gate_enforcing": True}
    state = {"registration": registration, "probe": probe, "after": [registration],
             "restart": {guard_api.fleet.FLEET_RESTART_REQUESTED_KEY: [{"pid": GUARD_REMOVAL_PID}]}}
    enumerations = []

    def enumerate_guard_api_registrations(**arguments):
        assert arguments == {"prune": False}
        enumerations.append(None)
        return [registration] if len(enumerations) % 2 else state["after"]

    with patch.object(guard_api.fleet, "running_consumers", side_effect=enumerate_guard_api_registrations), \
         patch.object(guard_api.fleet, "_classify_fleet_pid_identity",
                      return_value=guard_api.fleet._FLEET_PID_VERIFIED_LIVE), \
         patch.object(guard_api.fleet, "restart_stale", side_effect=lambda **kwargs: state["restart"]), \
         patch.object(guard_api, "probe_distribution_interpreter", side_effect=lambda *args: probe):
        green = guard_api.attest_distribution_api_consumers(target, settings={})
        assert len(green) == 1 and green[0]["healthy"] and green[0]["straggler_reason"] is None, green

        def assert_api_guard_rejects():
            enumerations.clear()
            rows = guard_api.attest_distribution_api_consumers(target, settings={})
            assert len(rows) == 1 and rows[0]["surface"] == f"api:{GUARD_REMOVAL_PID}"
            assert rows[0]["straggler_reason"] and not rows[0]["healthy"], rows

        yield state, assert_api_guard_rejects


def exercise_distribution_api_enforcement_guard_removal():
    with distribution_api_guard_fixture() as (state, rejects):
        mutant = compile_distribution_guard_removal(guard_api.attest_distribution_api_consumers, (
            ('candidate.get("gate_enforcing") is True', 'True'),))
        for unproven in (False, None, "true"):
            state["registration"]["gate_enforcing"] = unproven
            require_distribution_guard_mutant_killed(rejects, guard_api, "attest_distribution_api_consumers", mutant)
        del state["registration"]["gate_enforcing"]
        require_distribution_guard_mutant_killed(rejects, guard_api, "attest_distribution_api_consumers", mutant)


def exercise_distribution_api_probe_confirmation_guard_removal():
    with distribution_api_guard_fixture() as (state, rejects):
        state["probe"]["confirmed"] = False
        # Remove only the API health guard. The separate version proof must
        # remain unchanged; the rejection assertion also requires unhealthy.
        api_mutant = compile_distribution_guard_removal(guard_api.attest_distribution_api_consumers, (
            ('probe.get("confirmed")', 'True'),))
        require_distribution_guard_mutant_killed(rejects, guard_api, "attest_distribution_api_consumers", api_mutant)


def exercise_distribution_api_probe_enforcement_guard_removal():
    with distribution_api_guard_fixture() as (state, rejects):
        state["probe"]["gate_enforcing"] = False
        mutant = compile_distribution_guard_removal(guard_api.attest_distribution_api_consumers, (
            ('probe.get("gate_enforcing")', 'True'),))
        require_distribution_guard_mutant_killed(rejects, guard_api, "attest_distribution_api_consumers", mutant)


def exercise_distribution_api_restart_uses_current_registration():
    with distribution_api_guard_fixture() as (state, _rejects):
        guard_api.fleet.running_consumers.reset_mock()
        registration = state["registration"]
        target = {"version": registration["version"], "sha": registration["served_sha"]}
        rows = guard_api.attest_distribution_api_consumers(target, settings={})
        assert guard_api.fleet.running_consumers.call_count == 1
        assert len(rows) == 1 and rows[0]["healthy"], rows


def exercise_distribution_api_restart_confirmed_guard_removal():
    with distribution_api_guard_fixture() as (state, rejects):
        state["restart"] = {}
        mutant = compile_distribution_guard_removal(guard_api.attest_distribution_api_consumers, (
            ('restart_ok = bool(restarted.get(fleet.FLEET_RESTART_REQUESTED_KEY))', 'restart_ok = True'),))
        require_distribution_guard_mutant_killed(rejects, guard_api, "attest_distribution_api_consumers", mutant)


@contextlib.contextmanager
def distribution_daemon_guard_fixture():
    target = {"version": GUARD_REMOVAL_VERSION, "sha": GUARD_REMOVAL_SHA}
    consumer = {"pid": GUARD_REMOVAL_PID, "pid_create_time": GUARD_REMOVAL_CREATED,
                "supervision_ref": guard_daemon.MCP_HTTP_LAUNCHD_LABEL}
    status = {"pid": GUARD_REMOVAL_PID, "imported_version": GUARD_REMOVAL_VERSION,
              "metadata_version": GUARD_REMOVAL_VERSION, "ledger_path": GUARD_REMOVAL_LEDGER,
              "gate_enforcing": True, "served": {"sha": GUARD_REMOVAL_SHA, "dirty": False}}
    state = {"consumers": [consumer], "status": status,
             "identity": guard_daemon.fleet._FLEET_PID_VERIFIED_LIVE,
             "restart": {guard_daemon.fleet.FLEET_RESTART_REQUESTED_KEY: [{"pid": GUARD_REMOVAL_PID}]}}
    policy = {"mcp_endpoint": GUARD_REMOVAL_ENDPOINT, "probe_timeout_s": GUARD_REMOVAL_TIMEOUT}

    def answer_guard_daemon_rpc(endpoint, method, params, timeout):
        assert endpoint == GUARD_REMOVAL_ENDPOINT and timeout == GUARD_REMOVAL_TIMEOUT
        if method == "initialize":
            return {"serverInfo": {"version": GUARD_REMOVAL_VERSION}}
        return {"structuredContent": copy.deepcopy(status)}

    with patch.object(guard_daemon.fleet, "running_consumers", side_effect=lambda **kwargs: state["consumers"]), \
         patch.object(guard_daemon.fleet, "_classify_fleet_pid_identity", side_effect=lambda *args: state["identity"]), \
         patch.object(guard_daemon.fleet, "restart_stale", side_effect=lambda **kwargs: state["restart"]), \
         patch.object(guard_daemon.fleet, "fleet_control_restarts_enabled", return_value=False), \
         patch.object(guard_daemon, "request_distribution_mcp_rpc", side_effect=answer_guard_daemon_rpc), \
         patch.object(guard_daemon.mcp_fd_lifecycle, "attest_mcp_process_descriptors", return_value={"fd_ok": True}):
        green = guard_daemon.attest_distribution_daemons(target, settings=policy)
        assert len(green) == 1 and green[0]["healthy"] and green[0]["straggler_reason"] is None, green

        def assert_daemon_guard_rejects():
            rows = guard_daemon.attest_distribution_daemons(target, settings=policy)
            assert len(rows) == 1 and rows[0]["straggler_reason"] and not rows[0]["healthy"], rows

        yield state, assert_daemon_guard_rejects


def exercise_distribution_daemon_pid_match_guard_removal():
    with distribution_daemon_guard_fixture() as (state, rejects):
        state["status"]["pid"] = GUARD_REMOVAL_PID + 1
        mutant = compile_distribution_guard_removal(guard_daemon.prove_distribution_mcp_daemon, (
            ('if not pid or (consumer.get("pid") is not None and pid != consumer["pid"]):', 'if False:'),))
        require_distribution_guard_mutant_killed(rejects, guard_daemon, "prove_distribution_mcp_daemon", mutant)


def exercise_distribution_daemon_pid_identity_liveness_guard_removal():
    with distribution_daemon_guard_fixture() as (state, rejects):
        mutant = compile_distribution_guard_removal(guard_daemon.prove_distribution_mcp_daemon, (
            ('if identity != fleet._FLEET_PID_VERIFIED_LIVE:', 'if False:'),))
        for identity in (guard_daemon.fleet._FLEET_PID_UNVERIFIABLE, guard_daemon.fleet._FLEET_PID_GONE):
            state["identity"] = identity
            require_distribution_guard_mutant_killed(rejects, guard_daemon, "prove_distribution_mcp_daemon", mutant)


def exercise_distribution_daemon_no_owner_guard_removal():
    with distribution_daemon_guard_fixture() as (state, rejects):
        state["consumers"] = []
        mutant = compile_distribution_guard_removal(guard_daemon.attest_distribution_daemons, (
            ('restart_ok = False', 'restart_ok = True'),))
        require_distribution_guard_mutant_killed(rejects, guard_daemon, "attest_distribution_daemons", mutant)


def exercise_distribution_daemon_restart_confirmed_guard_removal():
    with distribution_daemon_guard_fixture() as (state, rejects):
        state["restart"] = {}
        mutant = compile_distribution_guard_removal(guard_daemon.attest_distribution_daemons, (
            ('restart_ok = bool(result.get(fleet.FLEET_RESTART_REQUESTED_KEY))', 'restart_ok = True'),))
        require_distribution_guard_mutant_killed(rejects, guard_daemon, "attest_distribution_daemons", mutant)


def exercise_distribution_capstone_armed_attestors_converge():
    """The full capstone must preserve green with both real process attestors."""
    with distribution_api_guard_fixture() as (api_state, _), \
         distribution_daemon_guard_fixture() as (daemon_state, _):
        consumers = [api_state["registration"], *daemon_state["consumers"]]
        target = {"version": GUARD_REMOVAL_VERSION, "sha": GUARD_REMOVAL_SHA}
        policy = {"mcp_endpoint": GUARD_REMOVAL_ENDPOINT, "probe_timeout_s": GUARD_REMOVAL_TIMEOUT}
        with patch.object(guard_api.fleet, "running_consumers", return_value=consumers), \
             patch.object(guard_api, "resolve_distribution_target", return_value=target), \
             patch.object(guard_api, "distribution_policy", return_value=policy), \
             patch.object(guard_api.setup, "discover_distribution_interpreters",
                          return_value={"interpreters": [], "gaps": []}), \
             patch.object(guard_api.consumer_audit, "audit_consumer_workspaces",
                          return_value={"consumers": [], "pruned_at_depth": []}), \
             patch.object(guard_api, "attest_distribution_host",
                          return_value=guard_api.distribution_surface_row("host", "host", True, True)):
            result = guard_api.distribute_fleet(check=True, settings={})
            assert result["exit_code"] == 0 and result["summary"]["stragglers"] == [], result
            assert {row["kind"] for row in result["surfaces"]} == {"api", "daemon", "host"}, result
            api_state["registration"]["gate_enforcing"] = False
            daemon_state["identity"] = guard_daemon.fleet._FLEET_PID_UNVERIFIABLE
            result = guard_api.distribute_fleet(check=True, settings={})
            assert result["exit_code"] != 0, result
            assert result["summary"]["stragglers"] == [
                f"api:{GUARD_REMOVAL_PID}", f"mcp-process:{GUARD_REMOVAL_PID}"], result


if __name__ == "__main__":
    exercise_distribution_api_enforcement_guard_removal()
    exercise_distribution_api_probe_confirmation_guard_removal()
    exercise_distribution_api_probe_enforcement_guard_removal()
    exercise_distribution_api_restart_uses_current_registration()
    exercise_distribution_api_restart_confirmed_guard_removal()
    exercise_distribution_daemon_pid_match_guard_removal()
    exercise_distribution_daemon_pid_identity_liveness_guard_removal()
    exercise_distribution_daemon_no_owner_guard_removal()
    exercise_distribution_daemon_restart_confirmed_guard_removal()
    exercise_distribution_capstone_armed_attestors_converge()
    print("OK API enforcement/confirmation/registration/restart; daemon PID/identity/liveness/no-owner/restart guard mutants killed")
