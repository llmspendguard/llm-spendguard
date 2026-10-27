"""Fail-closed descriptor lifecycle for the supervised HTTP daemon."""
import logging
import math
import resource
import threading
import time
from pathlib import Path

import psutil

from . import config

MCP_FD_SOFT_LIMIT_DEFAULT = 4096
MCP_FD_HIGH_WATER_DEFAULT = 0.75
MCP_FD_POLL_DEFAULT_S = 1.0
MCP_DEADLETTER_DRAIN_DEFAULT_S = 60.0
MCP_FD_CONFIG_SECTION = "mcp"
MCP_FD_LOG = logging.getLogger(__name__)
_mcp_next_deadletter_drain_at = 0.0
_mcp_deadletter_worker = None
_mcp_deadletter_schedule_lock = threading.Lock()
MCP_DEADLETTER_WORKER_NAME = "spendguard-mcp-deadletters"


def _reset_mcp_deadletter_scheduler_in_child():
    global _mcp_deadletter_schedule_lock, _mcp_deadletter_worker, _mcp_next_deadletter_drain_at
    _mcp_deadletter_schedule_lock = threading.Lock()
    _mcp_deadletter_worker = None
    _mcp_next_deadletter_drain_at = 0.0


if hasattr(config.os, "register_at_fork"):
    config.os.register_at_fork(after_in_child=_reset_mcp_deadletter_scheduler_in_child)


def service_mcp_spend_deadletters():
    """Schedule at most one retry worker; never admit writes on the accept thread."""
    global _mcp_next_deadletter_drain_at, _mcp_deadletter_worker
    interval = float(config._cfg_get(MCP_FD_CONFIG_SECTION, "deadletter_drain_interval_s",
                                     MCP_DEADLETTER_DRAIN_DEFAULT_S))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("invalid MCP deadletter drain interval")
    deadline = float(config._cfg_get(MCP_FD_CONFIG_SECTION, "deadletter_drain_deadline_s",
                                    config.LEDGER_READ_TIME_BUDGET_S))
    if not math.isfinite(deadline) or deadline <= 0:
        raise ValueError("invalid MCP deadletter drain deadline")
    if not _mcp_deadletter_schedule_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now < _mcp_next_deadletter_drain_at or (
                _mcp_deadletter_worker is not None and _mcp_deadletter_worker.is_alive()):
            return
        _mcp_next_deadletter_drain_at = now + interval
        from .budget import spend_event_retry_paths
        paths = tuple(path for path in spend_event_retry_paths() if path.exists() and path.stat().st_size)
        if not paths:
            return
        _mcp_deadletter_worker = threading.Thread(
            target=_run_mcp_deadletter_drain, args=(paths, deadline),
            name=MCP_DEADLETTER_WORKER_NAME, daemon=True)
        _mcp_deadletter_worker.start()
    except OSError:
        MCP_FD_LOG.exception("MCP spend retry scheduling failed; retrying at the next interval")
    finally:
        _mcp_deadletter_schedule_lock.release()


def _run_mcp_deadletter_drain(paths, deadline):
    """Bound writer admission; leave durable evidence for the next scheduled retry."""
    from .ledger_deadletter import drain_spend_deadletter_events
    from .ledger_read import ledger_request_scope
    try:
        with ledger_request_scope(deadline_s=deadline):
            report = drain_spend_deadletter_events(paths)
    except Exception:
        MCP_FD_LOG.exception("MCP spend retry drain failed; scheduled retry will recheck durable evidence")
        return
    if report["errors"] or report["retained"]:
        MCP_FD_LOG.error("MCP spend retry gaps: %s", report)
    elif report["committed"]:
        MCP_FD_LOG.info("MCP spend retries committed: %s", report["committed"])


class MCPDescriptorPressureError(RuntimeError):
    """The supervisor must replace a daemon that cannot recover descriptors."""


def mcp_descriptor_policy():
    """Read and validate operational policy before serving any requests."""
    target = int(config._cfg_get(MCP_FD_CONFIG_SECTION, "fd_soft_limit", MCP_FD_SOFT_LIMIT_DEFAULT))
    fraction = float(config._cfg_get(MCP_FD_CONFIG_SECTION, "fd_high_water_fraction", MCP_FD_HIGH_WATER_DEFAULT))
    interval = float(config._cfg_get(MCP_FD_CONFIG_SECTION, "fd_poll_interval_s", MCP_FD_POLL_DEFAULT_S))
    if target <= 0 or not math.isfinite(fraction) or not 0 < fraction < 1:
        raise ValueError("invalid MCP descriptor limit or high-water fraction")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("invalid MCP descriptor polling interval")
    return target, fraction, interval


def raise_mcp_descriptor_soft_limit(target):
    """Raise the soft limit within the inherited hard limit; never lower it."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    permitted = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and permitted > soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (permitted, hard))
    effective, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    MCP_FD_LOG.info("MCP descriptor soft limit requested=%s effective=%s", target, effective)
    return effective


def enforce_mcp_descriptor_headroom(fraction):
    """Recycle idle handles, then stop serving if pressure remains."""
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return
    process = psutil.Process()
    before = process.num_fds()
    if before < soft * fraction:
        return
    config.recycle_idle_ledger_connections()
    after = process.num_fds()
    MCP_FD_LOG.warning("MCP descriptor pressure: before=%s after=%s soft=%s", before, after, soft)
    if after >= soft * fraction:
        raise MCPDescriptorPressureError(
            f"MCP descriptor pressure persists after pool recycle: open={after} soft={soft}")


def attest_mcp_process_descriptors(pid):
    """Inspect the actual daemon, failing closed on unreadable or leaked ledger handles."""
    try:
        target, fraction, _interval = mcp_descriptor_policy()
        process = psutil.Process(pid)
        ledger_path = Path(config.db_path()).resolve()
        ledger_handles = [entry.fd for entry in process.open_files()
                          if Path(entry.path).resolve() == ledger_path]
        descriptor_count = process.num_fds()
        # Request-owned ledger handles must be closed when an idle probe finishes.
        okay = not ledger_handles and descriptor_count < target * fraction
        return {"fd_ok": okay, "descriptor_count": descriptor_count,
                "ledger_handles": ledger_handles,
                "reason": None if okay else "ledger handles remain open or descriptor headroom exhausted"}
    except Exception as exc:
        return {"fd_ok": False, "reason": f"descriptor proof unavailable: {type(exc).__name__}: {exc}"}
