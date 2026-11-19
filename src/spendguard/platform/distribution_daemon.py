"""Running-process MCP attestation; no fresh subprocess can stand in for a daemon."""
import inspect
import json
import os
import subprocess
import time
from typing import TypedDict
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .. import config, fleet, mcp_fd_lifecycle
from .launchd_labels import MCP_HTTP_LAUNCHD_LABEL, MCP_STDIO_LAUNCHD_LABEL

DISTRIBUTION_RPC_TIMEOUT_DEFAULT_S = 10.0
DISTRIBUTION_RPC_ID = "distribution-attestation"
DISTRIBUTION_MCP_PATH = "/mcp"
DISTRIBUTION_HTTP_SCHEME = "http"
DISTRIBUTION_VERSION_TOOL = "spendguard_version"
MCP_RESTART_TIMEOUT_DEFAULT_S = 30.0
MCP_RESTART_POLL_INTERVAL_S = 0.5
MCP_PROCESS_DISCOVERY_TIMEOUT_S = 5.0
MCP_LSOF_COMMAND = "lsof"
MCP_LAUNCHCTL_COMMAND = "launchctl"


class LiveMcpRestartResult(TypedDict):
    verified: bool
    loaded_sha: str | None
    intended_sha: str
    label_used: str | None
    attempts: int
    reason: str
    package_owned: bool | None


def distribution_daemon_settings(settings=None):
    """Resolve endpoint from the daemon's own defaults and operational overrides."""
    from ..mcp_server import serve_http
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
    from ..mcp_server import (MCP_READ_ONLY_ATTESTATION_META_KEY,
                             MCP_READ_ONLY_ATTESTATION_HEADER,
                             MCP_READ_ONLY_ATTESTATION_HEADER_VALUE)
    # Legacy servers self-restart after any POST. Their HEAD is safely rejected;
    # never send them an attestation that could turn a read-only check into repair.
    with urlopen(Request(endpoint, method="HEAD"), timeout=timeout) as response:
        if response.headers.get(MCP_READ_ONLY_ATTESTATION_HEADER) != MCP_READ_ONLY_ATTESTATION_HEADER_VALUE:
            raise ValueError("daemon does not prove support for mutation-free attestation")
    readonly_params = {**params, "_meta": {MCP_READ_ONLY_ATTESTATION_META_KEY: True}}
    document = {"jsonrpc": "2.0", "id": DISTRIBUTION_RPC_ID, "method": method, "params": readonly_params}
    request = Request(endpoint, data=json.dumps(document).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    if (result.get("id") != DISTRIBUTION_RPC_ID or result.get("jsonrpc") != "2.0"
            or "error" in result or "result" not in result):
        raise ValueError(f"invalid live MCP response: {result}")
    return result["result"]


def read_live_mcp_release_status(settings=None):
    """Read the running server itself; an unreachable or invalid responder raises."""
    policy = distribution_daemon_settings(settings)
    reply = request_distribution_mcp_rpc(policy["mcp_endpoint"], "tools/call",
        {"name": DISTRIBUTION_VERSION_TOOL, "arguments": {}}, policy["probe_timeout_s"])
    if reply.get("isError") or not isinstance(reply.get("structuredContent"), dict):
        raise ValueError("live MCP version response lacks structured status")
    return reply["structuredContent"]


def inspect_live_mcp_service(settings=None):
    """Identify the launchd owner of the endpoint listener without changing its plist."""
    policy = distribution_daemon_settings(settings)
    port = urlparse(policy["mcp_endpoint"]).port
    if port is None:
        return {"label_used": None, "package_owned": None, "reason": "MCP endpoint has no explicit port"}
    try:
        listeners = subprocess.run([MCP_LSOF_COMMAND, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=MCP_PROCESS_DISCOVERY_TIMEOUT_S, check=False)
        pids = {int(line) for line in listeners.stdout.splitlines() if line.strip().isdigit()}
        if len(pids) != 1:
            return {"label_used": None, "package_owned": None, "reason": "MCP listener identity is absent or ambiguous"}
        pid = next(iter(pids))
        jobs = subprocess.run([MCP_LAUNCHCTL_COMMAND, "list"], capture_output=True, text=True,
            timeout=MCP_PROCESS_DISCOVERY_TIMEOUT_S, check=False)
        if jobs.returncode:
            raise RuntimeError(jobs.stderr.strip() or "launchctl list failed")
        labels = [parts[2] for line in jobs.stdout.splitlines()
                  if len(parts := line.split()) >= 3 and parts[0] == str(pid)]
        if len(labels) != 1:
            return {"label_used": None, "package_owned": None, "reason": "MCP listener has no unique launchd owner"}
        registered = [row.get("supervision_ref") for row in fleet.running_consumers(prune=False)
                      if row.get("pid") == pid and row.get("supervision_ref") in labels
                      and fleet.launchd_consumer_label_loaded(row.get("supervision_ref")) is True]
        label = registered[0] if registered else labels[0]
        owned = label == MCP_HTTP_LAUNCHD_LABEL
        return {"label_used": label, "package_owned": owned,
                "reason": "package-owned MCP service" if owned else "installed MCP service is not package-owned"}
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        return {"label_used": None, "package_owned": None, "reason": f"MCP service ownership unproven: {exc}"}


def restart_and_verify_live_mcp(*, intended_sha, timeout_s=MCP_RESTART_TIMEOUT_DEFAULT_S, settings=None,
                                restart=True) -> LiveMcpRestartResult:
    """Restart the actual endpoint owner and verify the SHA reported over its transport."""
    service = inspect_live_mcp_service(settings)
    label = service["label_used"]
    failure = None
    if restart:
        if not label:
            failure = service["reason"]
        elif not fleet.fleet_control_restarts_enabled():
            failure = "control-plane restarts disabled by kill switch"
        else:
            try:
                outcome = fleet.kickstart_declared_launchd_consumer(label)
                if not outcome.get("ok"):
                    failure = f"kickstart failed: {outcome.get('detail')}"
            except Exception as exc:
                failure = f"kickstart failed: {type(exc).__name__}: {exc}"
    deadline = time.monotonic() + max(0, timeout_s)
    attempts = 0
    loaded = None
    probe_settings = distribution_daemon_settings(settings)
    while True:
        attempts += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                probe_settings["probe_timeout_s"] = min(probe_settings["probe_timeout_s"], remaining)
            loaded = (read_live_mcp_release_status(probe_settings).get("served") or {}).get("sha")
            if loaded and loaded == intended_sha and failure is None:
                return {"verified": True, "loaded_sha": loaded, "intended_sha": intended_sha,
                        "label_used": label, "attempts": attempts, "reason": "live MCP serves intended commit",
                        "package_owned": service["package_owned"]}
        except Exception as exc:
            failure_detail = f"live MCP status unavailable: {type(exc).__name__}: {exc}"
        else:
            failure_detail = "live MCP did not reload intended commit"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or failure:
            return {"verified": False, "loaded_sha": loaded, "intended_sha": intended_sha,
                    "label_used": label, "attempts": attempts, "reason": failure or failure_detail,
                    "package_owned": service["package_owned"]}
        time.sleep(min(MCP_RESTART_POLL_INTERVAL_S, remaining))


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
        # The PID-verified live responder supplies its own resolved ledger identity.
        # A checker can use another home/configuration, so its default is no evidence.
        ledger_path = status.get("ledger_path")
        if not isinstance(ledger_path, str) or not ledger_path or not os.path.isabs(ledger_path):
            raise ValueError("live MCP daemon ledger path is unavailable or not absolute")
        descriptors = mcp_fd_lifecycle.attest_mcp_process_descriptors(pid, ledger_path=ledger_path)
        row["fd_ok"] = descriptors["fd_ok"]
        if not row["fd_ok"]:
            reasons.append(descriptors.get("reason") or "daemon descriptor proof failed")
        gate_enforcing = status.get("gate_enforcing") is True
        if not gate_enforcing:
            reasons.append("live MCP daemon gate enforcement is unproven")
        row["healthy"] = bool(row["fd_ok"] and gate_enforcing)
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
    verification = None
    if not check and not dry_run:
        verification = restart_and_verify_live_mcp(intended_sha=target["sha"], settings=policy)
    for consumer in consumers:
        action = "none"
        restart_evidence = None
        restart_ok = True
        if consumer.get("endpoint") and verification is not None:
            restart_evidence = verification
            restart_ok = verification["verified"]
            action = "restart-verified" if restart_ok else "restart-verification-failed"
        elif not check:
            if dry_run:
                action = "would-restart"
            elif consumer.get("pid"):
                result = fleet.restart_stale(apply=True, consumer_id=consumer["pid"], all_supervised=True)
                restart_evidence = result
                restart_ok = bool(result.get(fleet.FLEET_RESTART_REQUESTED_KEY))
                action = "restart-requested" if restart_ok else "restart-request-failed"
            else:
                restart_ok = False
                action = "restart-request-failed"
        row = prove_distribution_mcp_daemon(consumer, target, policy)
        row["action_taken"] = action
        if restart_evidence is not None:
            row["evidence"]["restart"] = restart_evidence
        if not restart_ok:
            row["healthy"] = False
            row["straggler_reason"] = "; ".join(filter(None, (
                row["straggler_reason"],
                (f"live MCP loaded {verification['loaded_sha']} instead of intended {verification['intended_sha']}: "
                 f"{verification['reason']}" if verification is not None and consumer.get("endpoint") else
                 "required daemon restart could not be requested"))))
        rows.append(row)
    return rows


from . import platform_accessors as _platform_accessors

_platform_accessors.set_attest_distribution_daemons_resolver(
    lambda *args, **kwargs: attest_distribution_daemons(*args, **kwargs)
)
_platform_accessors.set_is_distribution_mcp_consumer_resolver(
    lambda *args, **kwargs: is_distribution_mcp_consumer(*args, **kwargs)
)
