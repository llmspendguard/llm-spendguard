"""Running-process MCP attestation; no fresh subprocess can stand in for a daemon."""
import inspect
import json
import os
from urllib.request import Request, urlopen

from . import config, fleet, mcp_fd_lifecycle
from .launchd_labels import MCP_HTTP_LAUNCHD_LABEL, MCP_STDIO_LAUNCHD_LABEL

DISTRIBUTION_RPC_TIMEOUT_DEFAULT_S = 10.0
DISTRIBUTION_RPC_ID = "distribution-attestation"
DISTRIBUTION_MCP_PATH = "/mcp"
DISTRIBUTION_HTTP_SCHEME = "http"
DISTRIBUTION_VERSION_TOOL = "spendguard_version"


def distribution_daemon_settings(settings=None):
    """Resolve endpoint from the daemon's own defaults and operational overrides."""
    from .mcp_server import serve_http
    defaults = inspect.signature(serve_http).parameters
    host = os.getenv("SPENDGUARD_MCP_HOST", str(defaults["host"].default))
    port = os.getenv("SPENDGUARD_MCP_PORT", str(defaults["port"].default))
    resolved = {
        "mcp_endpoint": config._cfg_get("distribute", "mcp_endpoint",
            f"{DISTRIBUTION_HTTP_SCHEME}://{host}:{port}{DISTRIBUTION_MCP_PATH}"),
        "probe_timeout_s": config._cfg_get("distribute", "probe_timeout_s", DISTRIBUTION_RPC_TIMEOUT_DEFAULT_S),
    }
    resolved.update(settings or {})
    return resolved


def request_distribution_mcp_rpc(endpoint, method, params, timeout):
    """Send a real, bounded JSON-RPC request to an already running HTTP daemon."""
    document = {"jsonrpc": "2.0", "id": DISTRIBUTION_RPC_ID, "method": method, "params": params}
    request = Request(endpoint, data=json.dumps(document).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    if (result.get("id") != DISTRIBUTION_RPC_ID or result.get("jsonrpc") != "2.0"
            or "error" in result or "result" not in result):
        raise ValueError(f"invalid live MCP response: {result}")
    return result["result"]


def prove_distribution_mcp_daemon(consumer, target, settings):
    """Require live protocol, version, frozen SHA, process identity and descriptor proofs."""
    row = {"surface": consumer.get("surface", f"mcp-process:{consumer.get('pid')}"),
           "kind": "daemon", "on_version": False, "healthy": False, "fd_ok": False,
           "action_taken": "none", "straggler_reason": None, "evidence": {}}
    reasons = []
    endpoint = consumer.get("endpoint")
    try:
        if not endpoint:
            raise ValueError("running MCP process has no independently probeable endpoint")
        initialize = request_distribution_mcp_rpc(endpoint, "initialize", {}, settings["probe_timeout_s"])
        reply = request_distribution_mcp_rpc(endpoint, "tools/call",
            {"name": DISTRIBUTION_VERSION_TOOL, "arguments": {}}, settings["probe_timeout_s"])
        if reply.get("isError"):
            raise ValueError(f"live version tool failed: {reply}")
        status = reply["structuredContent"]
        pid = status.get("pid")
        if not pid or (consumer.get("pid") is not None and pid != consumer["pid"]):
            raise ValueError("live MCP responder PID does not match enumerated daemon")
        identity = fleet._classify_fleet_pid_identity(pid, consumer.get("pid_create_time"))
        if identity != fleet._FLEET_PID_VERIFIED_LIVE:
            raise ValueError("daemon process identity cannot be proven")
        expected_version = target.get("version")
        imported = status.get("imported_version")
        metadata = status.get("metadata_version")
        announced = initialize["serverInfo"]["version"].partition("+")[0]
        row["on_version"] = bool(expected_version and imported == metadata == announced == expected_version
                                  and status.get("served", {}).get("sha") == target.get("sha")
                                  and status.get("served", {}).get("dirty") is False)
        if not row["on_version"]:
            reasons.append("live MCP version/metadata/SHA differs from deployed target")
        descriptors = mcp_fd_lifecycle.attest_mcp_process_descriptors(pid)
        row["fd_ok"] = descriptors["fd_ok"]
        if not row["fd_ok"]:
            reasons.append(descriptors.get("reason") or "daemon descriptor proof failed")
        row["healthy"] = row["fd_ok"]
        row["evidence"] = {"initialize": initialize, "version": status, "descriptors": descriptors}
    except Exception as exc:
        reasons.append(f"live MCP proof failed: {type(exc).__name__}: {exc}")
    row["straggler_reason"] = "; ".join(reasons) or None
    return row


def is_distribution_mcp_consumer(consumer):
    """Parse declared supervision identities and the exact MCP CLI argument."""
    labels = (MCP_HTTP_LAUNCHD_LABEL, MCP_STDIO_LAUNCHD_LABEL)
    return (consumer.get("supervision_ref") in labels or consumer.get("launchd_label") in labels
            or "mcp" in consumer.get("cmdline", "").split())


def attest_distribution_daemons(target, *, check=False, dry_run=False, settings=None):
    """Enumerate the required HTTP owner and all registered MCP processes; force then prove."""
    policy = distribution_daemon_settings(settings)
    consumers = [dict(row) for row in fleet.running_consumers(prune=False)
                 if is_distribution_mcp_consumer(row) and row.get("liveness") != fleet._FLEET_PID_GONE]
    http_consumers = [row for row in consumers
                      if row.get("supervision_ref") == MCP_HTTP_LAUNCHD_LABEL
                      or row.get("launchd_label") == MCP_HTTP_LAUNCHD_LABEL]
    for consumer in http_consumers:
        consumer["endpoint"] = policy["mcp_endpoint"]
    if not http_consumers:
        consumers.append({"surface": policy["mcp_endpoint"], "endpoint": policy["mcp_endpoint"]})
    rows = []
    for consumer in consumers:
        action = "none"
        restart_evidence = None
        restart_ok = True
        if not check:
            if dry_run:
                action = "would-restart"
            elif consumer.get("pid"):
                result = fleet.restart_stale(apply=True, consumer_id=consumer["pid"], all_supervised=True)
                restart_evidence = result
                restart_ok = bool(result.get(fleet.FLEET_RESTART_CONFIRMED_KEY))
                action = "restarted" if restart_ok else "restart-unconfirmed"
                label = consumer.get("supervision_ref")
                refreshed = [row for row in fleet.running_consumers(prune=False)
                             if label and row.get("supervision_ref") == label]
                if len(refreshed) == 1:
                    consumer = {**refreshed[0], "endpoint": consumer.get("endpoint")}
            else:
                restart_ok = False
                if fleet.fleet_control_restarts_enabled():
                    try:
                        restart_evidence = fleet.kickstart_declared_launchd_consumer(MCP_HTTP_LAUNCHD_LABEL)
                        restart_ok = bool(restart_evidence.get("ok")) and fleet.verify_launchd_consumer_green_return(
                            label=MCP_HTTP_LAUNCHD_LABEL, green_sha=target.get("sha"))
                    except Exception as exc:
                        restart_evidence = {"error": f"{type(exc).__name__}: {exc}"}
                refreshed = [item for item in fleet.running_consumers(prune=False)
                             if item.get("supervision_ref") == MCP_HTTP_LAUNCHD_LABEL]
                if len(refreshed) == 1:
                    consumer = {**refreshed[0], "endpoint": policy["mcp_endpoint"]}
                action = "restarted" if restart_ok else "restart-unconfirmed"
        row = prove_distribution_mcp_daemon(consumer, target, policy)
        row["action_taken"] = action
        if restart_evidence is not None:
            row["evidence"]["restart"] = restart_evidence
        if not restart_ok:
            row["healthy"] = False
            row["straggler_reason"] = "; ".join(filter(None, (
                row["straggler_reason"], "required daemon restart was not confirmed")))
        rows.append(row)
    return rows
