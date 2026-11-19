"""Install and safely migrate the package-owned MCP HTTP launchd job."""
import datetime
import pathlib
import plistlib
import shutil
import subprocess
import sys
from typing import TypedDict
from urllib.parse import urlparse

from . import distribution_daemon, fleet, manifest, release
from .launchd_labels import MCP_HTTP_LAUNCHD_LABEL

# launchd plists are authored for Apple's lenient plist parser, which accepts constructs the XML
# spec forbids (e.g. a `--` sequence inside a comment). Python's strict expat (plistlib) rejects
# those, so the Label is read with Apple's own tool instead of plistlib.
MCP_PLUTIL_COMMAND = "plutil"


class McpHttpInstallResult(TypedDict):
    status: str
    label: str
    port: int
    plist_path: str
    pythonpath: str | None
    reason: str
    document: dict
    actions: list[dict]


class McpHttpMigrationResult(TypedDict):
    migrated: bool
    verified: bool
    restored: bool
    reason: str
    plan: list[dict]


def mcp_http_install_pythonpath():
    """Use the imported checkout only when this interpreter is editable."""
    if manifest.inspect_current_interpreter_install_kind().get("install_kind") != manifest.MANIFEST_EDITABLE_KIND:
        return None
    import spendguard
    return str(pathlib.Path(spendguard.__file__).resolve().parent.parent)


def mcp_http_launchd_plist_path():
    return (pathlib.Path.home().joinpath(*fleet.FLEET_LAUNCHD_DIRECTORY_PARTS)
            / (MCP_HTTP_LAUNCHD_LABEL + fleet.FLEET_LAUNCHD_PLIST_SUFFIX))


def mcp_http_launchctl(*arguments):
    return subprocess.run([distribution_daemon.MCP_LAUNCHCTL_COMMAND, *arguments], capture_output=True,
                          text=True, timeout=fleet.FLEET_LAUNCHD_OPERATION_TIMEOUT_S, check=False)


def mcp_http_gui_domain():
    import os
    from ..manage.token_operations.codex_broker_owner import LAUNCHD_GUI_DOMAIN_TEMPLATE
    return LAUNCHD_GUI_DOMAIN_TEMPLATE % os.getuid()


def install_mcp_http_launchd_daemon(*, port=None, apply=True, settings=None) -> McpHttpInstallResult:
    """Install a KeepAlive service, preserving an already healthy owner without interruption."""
    from ..receipt import _spendguard_bin

    policy = distribution_daemon.distribution_daemon_settings(settings)
    resolved_port = port if port is not None else urlparse(policy["mcp_endpoint"]).port
    if resolved_port is None:
        raise ValueError("MCP endpoint has no explicit port")
    resolved_port = int(resolved_port)
    endpoint = urlparse(policy["mcp_endpoint"])
    if port is not None and endpoint.port != resolved_port:
        hostname = endpoint.hostname
        if not hostname:
            raise ValueError("MCP endpoint has no host")
        host = f"[{hostname}]" if ":" in hostname else hostname
        policy["mcp_endpoint"] = endpoint._replace(netloc=f"{host}:{resolved_port}").geturl()
    path = mcp_http_launchd_plist_path()
    pythonpath = mcp_http_install_pythonpath()
    environment = {"SPENDGUARD_NO_AUTOINSTALL": "1"}
    if pythonpath:
        environment["PYTHONPATH"] = pythonpath
    document = {"Label": MCP_HTTP_LAUNCHD_LABEL,
                "ProgramArguments": [_spendguard_bin(), "mcp", "--http", "--port", str(resolved_port)],
                "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True}
    result = {"status": "planned", "label": MCP_HTTP_LAUNCHD_LABEL, "port": resolved_port,
              "plist_path": str(path), "pythonpath": pythonpath, "reason": "install package-owned MCP HTTP service",
              "document": document, "actions": [{"action": "write", "path": str(path)},
                  {"action": "bootout", "label": MCP_HTTP_LAUNCHD_LABEL},
                  {"action": "bootstrap", "path": str(path)},
                  {"action": "kickstart", "label": MCP_HTTP_LAUNCHD_LABEL}]}
    if not apply:
        return result
    if sys.platform != "darwin":
        return {**result, "status": "failed", "reason": "launchd is unavailable on this platform"}
    if not fleet.fleet_control_restarts_enabled():
        return {**result, "status": "failed", "reason": "control-plane restarts disabled by kill switch"}
    service = distribution_daemon.inspect_live_mcp_service(policy)
    if service.get("package_owned") is True:
        try:
            distribution_daemon.read_live_mcp_release_status(policy)
        except Exception:
            pass
        else:
            return {**result, "status": "unchanged", "reason": "package-owned MCP HTTP service is healthy"}
    elif service.get("listener_present") is not False:
        return {**result, "status": "failed", "reason": service.get("reason", "listener ownership is unproven")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(document))
        fleet.bootout_declared_launchd_consumer(MCP_HTTP_LAUNCHD_LABEL)
        bootstrap = mcp_http_launchctl("bootstrap", mcp_http_gui_domain(), str(path))
        if bootstrap.returncode:
            raise RuntimeError(bootstrap.stderr.strip() or "bootstrap failed")
        kickstart = fleet.kickstart_declared_launchd_consumer(MCP_HTTP_LAUNCHD_LABEL)
        if not kickstart.get("ok"):
            raise RuntimeError(kickstart.get("detail") or "kickstart failed")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {**result, "status": "failed", "reason": str(exc)}
    return {**result, "status": "installed", "reason": "package-owned MCP HTTP service installed"}


def mcp_http_read_plist_label(path):
    """Read a launchd plist's Label via Apple's lenient parser, which launchd-valid plists require."""
    result = subprocess.run([MCP_PLUTIL_COMMAND, "-extract", "Label", "raw", "-o", "-", str(path)],
                            capture_output=True, text=True,
                            timeout=fleet.FLEET_LAUNCHD_OPERATION_TIMEOUT_S, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "cannot read foreign plist Label")
    return result.stdout.strip()


def mcp_http_foreign_plist_path(label):
    """Resolve launchd's actual job path and verify its declared identity."""
    result = mcp_http_launchctl("print", f"{mcp_http_gui_domain()}/{label}")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "cannot inspect foreign launchd job")
    paths = [line.partition("=")[2].strip() for line in result.stdout.splitlines()
             if line.partition("=")[0].strip() == "path"]
    if len(paths) != 1:
        raise ValueError("foreign launchd job has no unique plist path")
    path = pathlib.Path(paths[0])
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("foreign plist is not an absolute regular file")
    if mcp_http_read_plist_label(path) != label:
        raise ValueError("foreign plist label does not match launchd job")
    return path


def migrate_mcp_http_to_package_owned(*, apply=False, settings=None) -> McpHttpMigrationResult:
    """Retire a foreign job only after the replacement proves its source identity."""
    result = {"migrated": False, "verified": False, "restored": False, "reason": "", "plan": []}
    service = distribution_daemon.inspect_live_mcp_service(settings)
    if service.get("package_owned") is not False:
        return {**result, "reason": service.get("reason", "no foreign listener")}
    label = service.get("label_used")
    try:
        path = mcp_http_foreign_plist_path(label)
        if path.resolve() == mcp_http_launchd_plist_path().resolve():
            raise ValueError("foreign plist occupies the package-owned plist path")
        port = urlparse(distribution_daemon.distribution_daemon_settings(settings)["mcp_endpoint"]).port
        if port is None:
            raise ValueError("MCP endpoint has no explicit port")
        sha = release._git_src("rev-parse", "HEAD")
        if not sha:
            raise ValueError("current source SHA is unavailable")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        plan = [{"action": "backup", "path": str(path), "backup": str(backup)},
                {"action": "bootout", "label": label},
                {"action": "install", "label": MCP_HTTP_LAUNCHD_LABEL, "port": port},
                {"action": "verify", "intended_sha": sha}, {"action": "remove", "path": str(path)}]
        result["plan"] = plan
        if not apply:
            return {**result, "reason": "migration planned"}
        if not fleet.fleet_control_restarts_enabled():
            return {**result, "reason": "control-plane restarts disabled by kill switch"}
        with path.open("rb") as source, backup.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        shutil.copystat(path, backup)
        bootout = fleet.bootout_declared_launchd_consumer(label)
        if not bootout.get("ok"):
            return {**result, "reason": f"foreign bootout failed: {bootout.get('detail')}"}
        try:
            installed = install_mcp_http_launchd_daemon(port=port, settings=settings)
            if installed["status"] not in ("installed", "unchanged"):
                raise RuntimeError(installed["reason"])
            verification = distribution_daemon.restart_and_verify_live_mcp(intended_sha=sha, settings=settings)
            if verification.get("verified") is not True or verification.get("package_owned") is not True:
                raise RuntimeError(verification.get("reason") or "replacement ownership or verification failed")
            if path.read_bytes() != backup.read_bytes():
                raise RuntimeError("foreign plist changed during migration")
            path.unlink()
            return {**result, "migrated": True, "verified": True, "reason": "replacement verified; foreign plist retired"}
        except Exception as exc:
            fleet.bootout_declared_launchd_consumer(MCP_HTTP_LAUNCHD_LABEL)
            if not path.exists():
                shutil.copy2(backup, path)
            restored = mcp_http_launchctl("bootstrap", mcp_http_gui_domain(), str(path))
            kicked = fleet.kickstart_declared_launchd_consumer(label) if restored.returncode == 0 else {"ok": False}
            return {**result, "restored": bool(kicked.get("ok")), "reason": f"replacement failed: {exc}"}
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        return {**result, "reason": str(exc)}
