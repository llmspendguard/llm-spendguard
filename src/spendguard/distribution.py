"""Fleet convergence and fail-closed release attestation (no model calls)."""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
from pathlib import Path
import contextlib
import shutil
import stat
import subprocess
import sys
import tempfile

from . import config, consumer_audit, fleet, release, setup
from .distribution_daemon import attest_distribution_daemons

DISTRIBUTION_CONFIG_SECTION = "distribute"
DISTRIBUTION_PROBE_TIMEOUT_S = consumer_audit.CONSUMER_AUDIT_PROBE_TIMEOUT_SECONDS
DISTRIBUTION_INSTALL_TIMEOUT_S = fleet._CONSUMER_EDITABLE_REFRESH_TIMEOUT_S
DISTRIBUTION_CHECKOUT_DIRECTORY = "distribution-checkouts"
DISTRIBUTION_VERSION_SYMBOL = "__version__"
DISTRIBUTION_INTERPRETER_PROBE = '''import json,sys
preloaded = 'spendguard' in sys.modules
import spendguard
from importlib.metadata import version
from spendguard import release
enforcing = False
error = None
try:
 spendguard.require()
 enforcing = preloaded
except Exception as exc:
 error = str(exc)
identity = release.served_sha() or {}
print(json.dumps(dict(confirmed=True,version=spendguard.__version__,
 metadata_version=version('llm-spendguard'),sha=identity.get('sha'),
 dirty=identity.get('dirty',True),gate_enforcing=enforcing,error=error)))
'''


def distribution_policy():
    """Operational budgets are configurable; invalid policy cannot silently pass."""
    policy = {"probe_timeout_s": float(config._cfg_get(DISTRIBUTION_CONFIG_SECTION, "probe_timeout_s", DISTRIBUTION_PROBE_TIMEOUT_S)),
            "install_timeout_s": float(config._cfg_get(DISTRIBUTION_CONFIG_SECTION, "install_timeout_s", DISTRIBUTION_INSTALL_TIMEOUT_S))}
    if any(not math.isfinite(value) or value <= 0 for value in policy.values()):
        raise ValueError("distribution timeouts must be finite and positive")
    return policy


def resolve_distribution_target():
    """Resolve version from the blessed commit, never this process's installed version."""
    from . import _version
    pointer = release.green_pointer()
    if not pointer:
        raise ValueError("no deployed green pointer")
    repository = Path(__file__).resolve().parents[2]
    version_path = Path(_version.__file__).resolve().relative_to(repository).as_posix()
    source = release._git_src("show", f"{pointer['sha']}:{version_path}")
    if not source:
        raise ValueError("cannot read version from deployed SHA")
    assignments = ast.parse(source).body
    versions = [ast.literal_eval(node.value) for node in assignments if isinstance(node, ast.Assign)
                and any(isinstance(name, ast.Name) and name.id == DISTRIBUTION_VERSION_SYMBOL for name in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str) or not versions[0]:
        raise ValueError("deployed version is not uniquely established")
    if pointer.get("version") and pointer["version"] != versions[0]:
        raise ValueError("pointer version disagrees with deployed commit")
    return {"sha": pointer["sha"], "version": versions[0], "checkout": str(repository)}


def distribution_clean_environment():
    """Prevent the caller's PYTHONPATH from disguising the target installation."""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


@contextlib.contextmanager
def distribution_probe_environment():
    """Observe the real policy through a disposable, complete state snapshot.

    Startup hooks and doctor may register an interpreter or initialize a ledger.
    Keeping all state, including gate-disable markers, preserves their proof while
    confining those incidental writes to temporary storage for every probe.
    """
    with tempfile.TemporaryDirectory() as probe_state_directory:
        source = Path(config.HOME)
        snapshot = Path(probe_state_directory) / source.name
        if source.exists():
            shutil.copytree(source, snapshot, ignore=distribution_snapshot_runtime_endpoints)
        else:
            snapshot.mkdir()
        distribution_snapshot_configured_ledgers(snapshot)
        environment = distribution_clean_environment()
        environment["SPENDGUARD_HOME"] = str(snapshot)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        yield environment


def distribution_snapshot_runtime_endpoints(directory, names):
    """Runtime sockets/FIFOs/devices carry no stored policy and cannot be copied."""
    return [name for name in names
            if not (stat.S_ISREG((Path(directory) / name).stat().st_mode)
                    or stat.S_ISDIR((Path(directory) / name).stat().st_mode))]


def distribution_snapshot_configured_ledgers(snapshot):
    """Clone explicit SQLite targets and their complete WAL before observing them."""
    policy_path = snapshot / config.CONFIG_JSON.name
    if not policy_path.exists():
        return
    policy = json.loads(policy_path.read_text())
    budget_policy = policy.get("budget", {})
    for setting in ("db_path", "lane_queue_db_path"):
        configured_path = budget_policy.get(setting)
        if not configured_path:
            continue
        ledger_source = Path(configured_path).expanduser().resolve()
        ledger_destination = snapshot / setting / ledger_source.name
        ledger_destination.parent.mkdir()
        sources = [ledger_source, Path(str(ledger_source) + "-wal")]
        evidence = {path: fleet.sqlite_snapshot_source_evidence(path) for path in sources}
        for path, content in evidence.items():
            if content is not None:
                (ledger_destination.parent / path.name).write_bytes(content[1])
        if any(fleet.sqlite_snapshot_source_evidence(path) != content
               for path, content in evidence.items()):
            raise OSError(f"configured ledger changed during distribution snapshot: {ledger_source}")
        budget_policy[setting] = str(ledger_destination)
    if config.update_json(policy_path, lambda previous: policy) is None:
        raise OSError("could not isolate configured distribution ledgers")


def prepare_distribution_checkout(target, settings):
    """Use a clean matching tree or a detached worktree at the exact blessed commit."""
    checkout = Path(target["checkout"])
    timeout = settings["probe_timeout_s"]
    def distribution_git_query(path, *arguments):
        result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True,
                                timeout=timeout, check=True)
        return result.stdout.strip()
    if (distribution_git_query(checkout, "rev-parse", "HEAD") == target["sha"]
            and not distribution_git_query(checkout, "status", "--porcelain", "--untracked-files=no")):
        return str(checkout)
    configured = config._cfg_get(DISTRIBUTION_CONFIG_SECTION, "checkout_root")
    destination = (Path(configured).expanduser() if configured else Path(config.HOME) / DISTRIBUTION_CHECKOUT_DIRECTORY) / target["sha"]
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        distribution_git_query(checkout, "worktree", "add", "--detach", str(destination), target["sha"])
    if (distribution_git_query(destination, "rev-parse", "HEAD") != target["sha"]
            or distribution_git_query(destination, "status", "--porcelain", "--untracked-files=no")):
        raise ValueError(f"deployed checkout is not clean at target SHA: {destination}")
    return str(destination)


def probe_distribution_interpreter(python, settings):
    try:
        with distribution_probe_environment() as probe_environment:
            completed = subprocess.run([str(python), "-c", DISTRIBUTION_INTERPRETER_PROBE],
                                       env=probe_environment, capture_output=True, text=True,
                                       timeout=settings["probe_timeout_s"], check=False)
        if completed.returncode:
            return {"confirmed": False, "error": completed.stderr or completed.stdout}
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise ValueError("interpreter proof is not an object")
        return result
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"confirmed": False, "error": str(exc)}


def distribution_interpreter_on_target(probe, target):
    return bool(probe.get("confirmed") and probe.get("version") == target["version"]
                and probe.get("metadata_version") == target["version"]
                and probe.get("sha") == target["sha"] and probe.get("dirty") is False)


def distribution_surface_row(surface, kind, on_version=False, healthy=False, fd_ok=None, action="none", reason=None, **evidence):
    return {"surface": str(surface), "kind": kind, "on_version": bool(on_version), "healthy": bool(healthy),
            "fd_ok": fd_ok, "action_taken": action,
            "straggler_reason": reason or (None if on_version and healthy and fd_ok is not False else "identity or health unproven"),
            **evidence}


def attest_distribution_host(target, settings):
    probe = probe_distribution_interpreter(sys.executable, settings)
    try:
        with distribution_probe_environment() as probe_environment:
            result = subprocess.run([sys.executable, "-m", "spendguard", "doctor"],
                                    env=probe_environment, capture_output=True, text=True,
                                    timeout=settings["probe_timeout_s"], check=False)
        healthy = bool(result.returncode == 0 and probe.get("confirmed") and probe.get("gate_enforcing"))
        reason = ("host gate proof unconfirmed" if not probe.get("confirmed") else
                  "host gate not enforcing" if not probe.get("gate_enforcing") else None)
        return distribution_surface_row("host", "host", distribution_interpreter_on_target(probe, target),
                                        healthy, reason=reason, probe=probe, doctor_exit_code=result.returncode,
                                        doctor_output=result.stdout + result.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return distribution_surface_row("host", "host", reason=f"doctor unproven: {exc}", probe=probe)


def attest_distribution_api_consumers(target, *, check=False, dry_run=False, settings=None):
    """Require the process's own governance registration plus fresh installation proof."""
    from .distribution_daemon import is_distribution_mcp_consumer
    policy = distribution_policy() if settings is None else settings
    before = fleet.running_consumers(prune=False)
    rows = []
    for consumer in before:
        if is_distribution_mcp_consumer(consumer) or consumer.get("liveness") == fleet._FLEET_PID_GONE:
            continue  # daemon helper owns live MCP proof
        surface = f"api:{consumer['pid']}"
        action = "none"
        candidate = consumer
        restart_ok = True
        restarted = None
        if not check and not dry_run:
            restarted = fleet.restart_stale(apply=True, consumer_id=str(consumer["pid"]), all_supervised=True)
            action = "restart-requested"
            restart_ok = bool(restarted.get(fleet.FLEET_RESTART_CONFIRMED_KEY))
            # Re-enumerate; PID disappearance alone is never a successful restart.
            after = fleet.running_consumers(prune=False)
            matching = [item for item in after if item.get("pid") == consumer["pid"]
                        or (consumer.get("supervision_ref") and item.get("supervision_ref") == consumer.get("supervision_ref"))]
            candidate = matching[0] if len(matching) == 1 else {}
        elif dry_run and not check:
            action = "would-restart"
        python = candidate.get("python_executable")
        probe = probe_distribution_interpreter(python, policy) if python else {"confirmed": False, "error": "registered interpreter unavailable"}
        on_version = (candidate.get("served_sha") == target["sha"] and candidate.get("version") == target["version"]
                      and distribution_interpreter_on_target(probe, target))
        identity = fleet._classify_fleet_pid_identity(candidate.get("pid"), candidate.get("pid_create_time")) if candidate else None
        healthy = bool(candidate.get("identity_verified") and candidate.get("liveness") == fleet._FLEET_PID_VERIFIED_LIVE
                       and identity == fleet._FLEET_PID_VERIFIED_LIVE and candidate.get("dirty") is False
                       and candidate.get("gate_enforcing") is True
                       and probe.get("confirmed") and probe.get("gate_enforcing") and restart_ok)
        reason = "required API consumer restart was not confirmed" if not restart_ok else None
        rows.append(distribution_surface_row(surface, "api", on_version, healthy, action=action, reason=reason,
                                             registration=candidate, probe=probe, restart=restarted))
    return rows


def distribute_fleet(*, check=False, roots=None, dry_run=False, settings=None):
    """Idempotent scheduler/library entry. Any missing proof is a named straggler."""
    rows = []
    target = None
    audit_pruning = []
    try:
        target = resolve_distribution_target()
        policy = distribution_policy()
        audit_settings = config.consumer_audit_settings() if settings is None else settings
        try:
            audit = consumer_audit.audit_consumer_workspaces(target["sha"], roots=roots, auto_arm=False,
                                                             dry_run=True, settings=audit_settings)
        except Exception as exc:
            rows.append(distribution_surface_row("consumer-audit", "enumeration", reason=str(exc)))
            audit = {"consumers": [], "pruned_at_depth": []}
        try:
            inventory = setup.discover_distribution_interpreters(roots=roots, settings=audit_settings)
        except Exception as exc:
            rows.append(distribution_surface_row("interpreter-discovery", "enumeration", reason=str(exc)))
            inventory = {"interpreters": [], "gaps": []}
        interpreters = {str(Path(item["python"]).absolute()): item for item in inventory["interpreters"]}
        for consumer in audit["consumers"]:
            if consumer.get("final_verdict") == consumer_audit.CONSUMER_AUDIT_EXEMPT:
                continue
            if consumer.get("python"):
                interpreters.setdefault(str(Path(consumer["python"]).absolute()),
                                        {"python": consumer["python"], "venv": consumer.get("venv"), "kind": "venv"})
            else:
                rows.append(distribution_surface_row(consumer["repo"], "api", reason=consumer.get("hint") or "no interpreter"))
        for gap in inventory["gaps"]:
            rows.append(distribution_surface_row(gap["surface"], "enumeration", reason=gap["reason"]))
        # Shared discovery walks beyond audit's workspace depth and names any real gaps.
        audit_pruning = audit["pruned_at_depth"]
        checkout = None
        checkout_error = None
        if not check and not dry_run:
            try:
                checkout = prepare_distribution_checkout(target, policy)
            except Exception as exc:
                checkout_error = str(exc)
        for python, item in sorted(interpreters.items()):
            action = "none"
            error = checkout_error
            if not check and not dry_run and checkout:
                try:
                    fleet._refresh_editable_install(python, checkout, policy["install_timeout_s"])
                    action = "reinstalled"
                    hook_code = (setup.install_hook(item["venv"], install_pkg=False) if item.get("venv")
                                 else setup.install_hook(user=True, python=python, install_pkg=False))
                    if hook_code:
                        error = f"gate arm exited {hook_code}"
                except Exception as exc:
                    error = f"convergence failed: {exc}"
            elif dry_run and not check:
                action = "would-reinstall"
            probe = probe_distribution_interpreter(python, policy)
            rows.append(distribution_surface_row(python, item["kind"], distribution_interpreter_on_target(probe, target),
                                                 probe.get("confirmed") and probe.get("gate_enforcing") and not error,
                                                 action=action, reason=error, probe=probe))
        # Retain audit arming rather than growing a parallel workspace policy.
        if not check and not dry_run:
            try:
                consumer_audit.audit_consumer_workspaces(target["sha"], roots=roots, auto_arm=True,
                                                          dry_run=False, settings=audit_settings)
            except Exception as exc:
                rows.append(distribution_surface_row("consumer-arming", "gate", reason=str(exc)))
        for surface, proof in (("api-inventory", attest_distribution_api_consumers),
                               ("mcp-daemon", attest_distribution_daemons)):
            try:
                rows.extend(proof(target, check=check, dry_run=dry_run, settings=policy))
            except Exception as exc:
                rows.append(distribution_surface_row(getattr(exc, "surface", surface), "enumeration", reason=str(exc)))
        try:
            rows.append(attest_distribution_host(target, policy))
        except Exception as exc:
            rows.append(distribution_surface_row("host", "host", reason=str(exc)))
    except Exception as exc:
        rows.append(distribution_surface_row("distribution", "attestation", reason=f"proof unavailable: {type(exc).__name__}: {exc}"))
    stragglers = [row["surface"] for row in rows if row["straggler_reason"]]
    return {"target": target, "check": check, "dry_run": dry_run, "surfaces": rows,
            "enumeration": {"audit_pruned_at_depth": audit_pruning},
            "summary": {"converged": len(rows) - len(stragglers), "total": len(rows), "stragglers": stragglers},
            "exit_code": int(bool(stragglers) or not rows)}


def render_distribution_table(result):
    fields = ("surface", "kind", "on_version", "healthy", "fd_ok", "action_taken", "straggler_reason")
    lines = [" | ".join(fields)]
    lines.extend(" | ".join(str(row[field]) for field in fields) for row in result["surfaces"])
    lines.append(json.dumps({"summary": result["summary"], "exit_code": result["exit_code"]}, sort_keys=True))
    return "\n".join(lines)


def distribute_command(argv=None):
    parser = argparse.ArgumentParser(prog="spendguard distribute")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--roots", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = distribute_fleet(check=args.check, roots=args.roots, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_distribution_table(result))
    return result["exit_code"]
