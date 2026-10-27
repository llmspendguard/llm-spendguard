"""Offline red-to-green MCP process proofs, including deliberate check removal."""
import ast
import copy
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import distribution_daemon as daemon
from spendguard import mcp_fd_lifecycle as lifecycle
from spendguard import mcp_server

DAEMON_FIXTURE_PID = 217
DAEMON_FIXTURE_CREATED = 101.0
DAEMON_FIXTURE_VERSION = "fixture-current"
DAEMON_FIXTURE_OLD_VERSION = "fixture-old"
DAEMON_FIXTURE_SHA = "fixture-current-sha"
DAEMON_FIXTURE_OLD_SHA = "fixture-old-sha"
DAEMON_FIXTURE_ENDPOINT = "http://fixture.invalid/mcp"
DAEMON_FIXTURE_SOFT_LIMIT = 100
DAEMON_FIXTURE_FRACTION = 0.75
DAEMON_FIXTURE_DESCRIPTOR_COUNT = 4
DAEMON_FIXTURE_FD = 7
DAEMON_FIXTURE_LEDGER_PATH = "/fixture/daemon/spend.db"


def build_distribution_daemon_check_mutant(axis):
    """Remove the relevant production check from its complete AST, without editing source."""
    tree = ast.parse(inspect.getsource(daemon.prove_distribution_mcp_daemon))
    class DistributionDaemonCheckRemoval(ast.NodeTransformer):
        def visit_Assign(self, node):
            self.generic_visit(node)
            if axis in ("sha", "version", "metadata", "dirty", "fd"):
                field = "fd_ok" if axis == "fd" else "on_version"
                if any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                       and target.slice.value == field for target in node.targets):
                    node.value = ast.Constant(value=True)
            if axis == "gate" and any(isinstance(target, ast.Name) and target.id == "gate_enforcing"
                                      for target in node.targets):
                node.value = ast.Constant(value=True)
            return node

        def visit_ExceptHandler(self, node):
            if axis == "live-probe":
                node.body = [ast.Pass()]
            return node

    mutant_tree = ast.fix_missing_locations(DistributionDaemonCheckRemoval().visit(tree))
    namespace = dict(vars(daemon))
    exec(compile(mutant_tree, inspect.getfile(daemon.prove_distribution_mcp_daemon), "exec"), namespace)
    return namespace[daemon.prove_distribution_mcp_daemon.__name__]


def exercise_distribution_daemon_red_green():
    target = {"version": DAEMON_FIXTURE_VERSION, "sha": DAEMON_FIXTURE_SHA}
    consumer = {"pid": DAEMON_FIXTURE_PID, "pid_create_time": DAEMON_FIXTURE_CREATED,
                "supervision_ref": daemon.MCP_HTTP_LAUNCHD_LABEL}
    clean = {"pid": DAEMON_FIXTURE_PID, "imported_version": DAEMON_FIXTURE_VERSION,
             "metadata_version": DAEMON_FIXTURE_VERSION, "ledger_path": DAEMON_FIXTURE_LEDGER_PATH,
             "gate_enforcing": True,
             "served": {"sha": DAEMON_FIXTURE_SHA, "dirty": False}}
    state = {"status": copy.deepcopy(clean), "broken_probe": False, "leaked": False}
    policy = {"mcp_endpoint": DAEMON_FIXTURE_ENDPOINT}

    def answer_distribution_fixture_rpc(endpoint, method, params, timeout):
        assert endpoint == DAEMON_FIXTURE_ENDPOINT and timeout > 0
        if state["broken_probe"]:
            raise OSError("fixture live probe failure")
        if method == "initialize":
            return {"serverInfo": {"version": DAEMON_FIXTURE_VERSION}}
        assert method == "tools/call" and params["name"] == daemon.DISTRIBUTION_VERSION_TOOL
        return {"structuredContent": state["status"], "isError": False}

    def repair_distribution_fixture_daemon(**arguments):
        assert arguments == {"apply": True, "consumer_id": DAEMON_FIXTURE_PID, "all_supervised": True}
        state.update(status=copy.deepcopy(clean), broken_probe=False, leaked=False)
        return {daemon.fleet.FLEET_RESTART_CONFIRMED_KEY: [{"pid": DAEMON_FIXTURE_PID}]}

    def inspect_distribution_fixture_fds(pid, ledger_path=None):
        assert pid == DAEMON_FIXTURE_PID
        assert ledger_path == DAEMON_FIXTURE_LEDGER_PATH
        return {"fd_ok": not state["leaked"], "reason": "fixture leaked ledger fd"}

    mutations = (
        ("sha", lambda: state["status"]["served"].update(sha=DAEMON_FIXTURE_OLD_SHA)),
        ("version", lambda: state["status"].update(imported_version=DAEMON_FIXTURE_OLD_VERSION)),
        ("metadata", lambda: state["status"].update(metadata_version=DAEMON_FIXTURE_OLD_VERSION)),
        ("dirty", lambda: state["status"]["served"].update(dirty=True)),
        ("live-probe", lambda: state.update(broken_probe=True)),
        ("fd", lambda: state.update(leaked=True)),
        ("gate", lambda: state["status"].update(gate_enforcing=False)),
    )
    with patch.object(daemon.fleet, "running_consumers", return_value=[consumer]), \
         patch.object(daemon.fleet, "_classify_fleet_pid_identity", return_value=daemon.fleet._FLEET_PID_VERIFIED_LIVE), \
         patch.object(daemon, "request_distribution_mcp_rpc", side_effect=answer_distribution_fixture_rpc), \
         patch.object(daemon.fleet, "restart_stale", side_effect=repair_distribution_fixture_daemon) as restart, \
         patch.object(lifecycle, "attest_mcp_process_descriptors", side_effect=inspect_distribution_fixture_fds):
        for label, mutate in mutations:
            mutate()
            red = daemon.attest_distribution_daemons(target, check=True, settings=policy)
            assert len(red) == 1 and red[0]["straggler_reason"], label
            restart.assert_not_called()
            green = daemon.attest_distribution_daemons(target, settings=policy)
            assert len(green) == 1 and green[0]["straggler_reason"] is None, label
            restart.reset_mock()
        assert daemon.attest_distribution_daemons(target, check=True, settings=policy)[0]["straggler_reason"] is None
        with patch.object(daemon.fleet, "restart_stale", return_value={}):
            failed_restart = daemon.attest_distribution_daemons(target, settings=policy)[0]
            assert failed_restart["straggler_reason"] and not failed_restart["healthy"]
        # Removing each input check makes its corresponding red assertion fail.
        for label, mutate in mutations:
            mutate()
            mutant = build_distribution_daemon_check_mutant(label)
            with patch.object(daemon, "prove_distribution_mcp_daemon", side_effect=mutant):
                try:
                    rows = daemon.attest_distribution_daemons(target, check=True, settings=policy)
                    assert rows[0]["straggler_reason"], label
                except AssertionError:
                    pass
                else:
                    raise AssertionError(f"removed {label} check survived red assertion")
            repair_distribution_fixture_daemon(apply=True, consumer_id=DAEMON_FIXTURE_PID, all_supervised=True)
    print("[OK] daemon old SHA/version/metadata, failed live probe, leaked fd red→restart→green; load-bearing proof")


def exercise_distribution_descriptor_external_process():
    ledger = Path(DAEMON_FIXTURE_LEDGER_PATH).resolve()
    process = SimpleNamespace(open_files=lambda: [SimpleNamespace(path=str(ledger), fd=DAEMON_FIXTURE_FD)],
                              num_fds=lambda: DAEMON_FIXTURE_DESCRIPTOR_COUNT)
    with patch.object(lifecycle.psutil, "Process", return_value=process), \
         patch.object(lifecycle.config, "db_path", return_value=DAEMON_FIXTURE_LEDGER_PATH), \
         patch.object(lifecycle, "mcp_descriptor_policy", return_value=(
             DAEMON_FIXTURE_SOFT_LIMIT, DAEMON_FIXTURE_FRACTION, 1.0)):
        assert not lifecycle.attest_mcp_process_descriptors(DAEMON_FIXTURE_PID)["fd_ok"]
        process.open_files = lambda: []
        assert lifecycle.attest_mcp_process_descriptors(DAEMON_FIXTURE_PID)["fd_ok"]
    print("[OK] external descriptor proof sees configured ledger leak and recovery")


def exercise_distribution_daemon_enumeration():
    target = {"version": DAEMON_FIXTURE_VERSION, "sha": DAEMON_FIXTURE_SHA}
    consumers = [{"pid": DAEMON_FIXTURE_PID, "supervision_ref": daemon.MCP_STDIO_LAUNCHD_LABEL}]
    with patch.object(daemon.fleet, "running_consumers", return_value=consumers), \
         patch.object(daemon.fleet, "_classify_fleet_pid_identity", return_value=daemon.fleet._FLEET_PID_UNVERIFIABLE), \
         patch.object(lifecycle, "attest_mcp_process_descriptors", return_value={"fd_ok": False}), \
         patch.object(daemon, "request_distribution_mcp_rpc", side_effect=OSError("offline")):
        rows = daemon.attest_distribution_daemons(target, check=True,
                                                  settings={"mcp_endpoint": DAEMON_FIXTURE_ENDPOINT})
        assert len(rows) == 2 and all(row["straggler_reason"] for row in rows)
        assert {row["surface"] for row in rows} == {f"mcp-process:{DAEMON_FIXTURE_PID}", DAEMON_FIXTURE_ENDPOINT}
    print("[OK] stdio daemon and required HTTP endpoint cannot be silently omitted")


def exercise_distribution_wire_rpc():
    reply = {"jsonrpc": "2.0", "id": daemon.DISTRIBUTION_RPC_ID, "result": {"live": True}}
    class DistributionWireFixture:
        headers = {mcp_server.MCP_READ_ONLY_ATTESTATION_HEADER:
                   mcp_server.MCP_READ_ONLY_ATTESTATION_HEADER_VALUE}
        def __enter__(self):
            return self

        def __exit__(self, *exception):
            return False

        def read(self):
            return json.dumps(reply).encode()

    with patch.object(daemon, "urlopen", return_value=DistributionWireFixture()) as request:
        assert daemon.request_distribution_mcp_rpc(DAEMON_FIXTURE_ENDPOINT, "initialize", {}, 1.0) == {"live": True}
        assert json.loads(request.call_args.args[0].data)["method"] == "initialize"
        assert json.loads(request.call_args.args[0].data)["params"]["_meta"][
            mcp_server.MCP_READ_ONLY_ATTESTATION_META_KEY] is True
        assert request.call_args_list[0].args[0].method == "HEAD"
        DistributionWireFixture.headers = {}
        request.reset_mock()
        try:
            daemon.request_distribution_mcp_rpc(DAEMON_FIXTURE_ENDPOINT, "initialize", {}, 1.0)
        except ValueError:
            pass
        else:
            raise AssertionError("legacy HTTP daemon accepted without read-only capability")
        assert request.call_count == 1 and request.call_args.args[0].method == "HEAD"
    print("[OK] live proof uses HTTP JSON-RPC request seam, without network in tests")


def exercise_distribution_probe_prevents_http_self_restart():
    """A stale daemon must remain observable throughout a read-only check."""
    from spendguard import release
    with patch.object(mcp_server, "_arm_and_verify_mcp_daemon_gate"):
        handler_class = mcp_server.build_mcp_http_handler()
    handler = object.__new__(handler_class)
    handler.path = daemon.DISTRIBUTION_MCP_PATH
    handler.wfile = io.BytesIO()
    handler.connection = SimpleNamespace()
    with patch.object(handler_class, "send_response") as head_status, \
         patch.object(handler_class, "send_header") as head_header, \
         patch.object(handler_class, "end_headers"), \
         patch.object(mcp_server, "handle") as head_dispatch, \
         patch.object(release, "should_respawn") as head_respawn:
        handler.do_HEAD()
        head_status.assert_called_once_with(mcp_server.HTTPStatus.OK)
        head_header.assert_any_call(mcp_server.MCP_READ_ONLY_ATTESTATION_HEADER,
                                    mcp_server.MCP_READ_ONLY_ATTESTATION_HEADER_VALUE)
        head_dispatch.assert_not_called()
        head_respawn.assert_not_called()
    with patch.object(handler_class, "_send"), \
         patch.object(mcp_server, "_mcp_http_request_ledger_lifetime"), \
         patch.object(mcp_server, "handle", return_value={}), \
         patch.object(mcp_server, "_auto_respawn_enabled", return_value=True), \
         patch.object(release, "should_respawn", return_value=True), \
         patch("os._exit") as exit_process:
        for method, params in (("initialize", {}), ("tools/call", {"name": daemon.DISTRIBUTION_VERSION_TOOL})):
            params["_meta"] = {mcp_server.MCP_READ_ONLY_ATTESTATION_META_KEY: True}
            payload = json.dumps({"jsonrpc": "2.0", "id": daemon.DISTRIBUTION_RPC_ID,
                                  "method": method, "params": params}).encode()
            handler.headers = {mcp_server.HTTP_CONTENT_LENGTH_HEADER: str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler.do_POST()
        exit_process.assert_not_called()
        payload = json.dumps({"jsonrpc": "2.0", "id": daemon.DISTRIBUTION_RPC_ID,
                              "method": "initialize", "params": {}}).encode()
        handler.headers = {mcp_server.HTTP_CONTENT_LENGTH_HEADER: str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.do_POST()
        exit_process.assert_called_once_with(0)
    print("[OK] attestation initialize/version requests cannot restart stale HTTP daemon; ordinary self-heal preserved")


def exercise_distribution_missing_http_owner_restart():
    target = {"version": DAEMON_FIXTURE_VERSION, "sha": DAEMON_FIXTURE_SHA}
    consumer = {"pid": DAEMON_FIXTURE_PID, "pid_create_time": DAEMON_FIXTURE_CREATED,
                "supervision_ref": daemon.MCP_HTTP_LAUNCHD_LABEL}
    proof = {"surface": DAEMON_FIXTURE_ENDPOINT, "kind": "daemon", "on_version": True,
             "healthy": True, "fd_ok": True, "straggler_reason": None, "evidence": {}}
    with patch.object(daemon.fleet, "running_consumers", side_effect=[[], [consumer]]), \
         patch.object(daemon.fleet, "fleet_control_restarts_enabled", return_value=True), \
         patch.object(daemon.fleet, "kickstart_declared_launchd_consumer", return_value={"ok": True}) as kick, \
         patch.object(daemon.fleet, "verify_launchd_consumer_green_return", return_value=True) as verify, \
         patch.object(daemon, "prove_distribution_mcp_daemon", return_value=proof) as attest:
        rows = daemon.attest_distribution_daemons(target, settings={"mcp_endpoint": DAEMON_FIXTURE_ENDPOINT})
        kick.assert_called_once_with(daemon.MCP_HTTP_LAUNCHD_LABEL)
        verify.assert_called_once_with(label=daemon.MCP_HTTP_LAUNCHD_LABEL, green_sha=DAEMON_FIXTURE_SHA)
        assert attest.call_args.args[0]["pid_create_time"] == DAEMON_FIXTURE_CREATED
        assert rows[0]["action_taken"] == "restarted" and not rows[0]["straggler_reason"]
    print("[OK] missing HTTP owner uses shared launchd restart and fresh registry identity")


if __name__ == "__main__":
    exercise_distribution_daemon_red_green()
    exercise_distribution_descriptor_external_process()
    exercise_distribution_daemon_enumeration()
    exercise_distribution_wire_rpc()
    exercise_distribution_probe_prevents_http_self_restart()
    exercise_distribution_missing_http_owner_restart()
