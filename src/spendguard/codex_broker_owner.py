"""Launchd ownership for the host-wide Codex broker; consumers only install/kickstart and attach."""
import os
import pathlib
import plistlib
import subprocess
import sys

from . import config
from .launchd_labels import CODEX_BROKER_OWNER_LAUNCHD_LABEL


CODEX_BROKER_OWNER_ROLE = "codex-broker-owner"
CODEX_BROKER_OWNER_PLIST_SUFFIX = ".plist"
CODEX_BROKER_OWNER_LAUNCHD_DIRECTORY_PARTS = ("Library", "LaunchAgents")
CODEX_BROKER_OWNER_KICKSTART_TIMEOUT_S = 15
LAUNCHD_GUI_DOMAIN_TEMPLATE = "gui/%d"


def run_declared_launchd_action(action_arguments, label):
    """Run one launchctl action against an explicitly declared label; never discovers by command line."""
    domain_label = "%s/%s" % (LAUNCHD_GUI_DOMAIN_TEMPLATE % os.getuid(), label)
    return subprocess.run(["launchctl", *action_arguments, domain_label], capture_output=True, text=True,
                          timeout=CODEX_BROKER_OWNER_KICKSTART_TIMEOUT_S)


def launchd_codex_broker_owner_supported():
    """Return whether this platform can supervise the dedicated broker owner."""
    return sys.platform == "darwin"


def codex_broker_owner_plist_path():
    return pathlib.Path.home().joinpath(*CODEX_BROKER_OWNER_LAUNCHD_DIRECTORY_PARTS) / (
        CODEX_BROKER_OWNER_LAUNCHD_LABEL + CODEX_BROKER_OWNER_PLIST_SUFFIX)


def codex_broker_owner_program_arguments():
    return [sys.executable, "-m", "spendguard.codex_broker_owner"]


def install_codex_broker_owner_launchd():
    """Write the package-owned KeepAlive job idempotently; never unload or kill an existing process."""
    path = codex_broker_owner_plist_path()
    config.HOME.mkdir(parents=True, exist_ok=True)
    log_path = config.HOME / (CODEX_BROKER_OWNER_ROLE + ".log")
    document = {"Label": CODEX_BROKER_OWNER_LAUNCHD_LABEL,
                "ProgramArguments": codex_broker_owner_program_arguments(),
                "RunAtLoad": True, "KeepAlive": True,
                "StandardErrorPath": str(log_path), "StandardOutPath": str(log_path)}
    encoded = plistlib.dumps(document)
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != encoded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    return path


def ensure_codex_broker_owner_running():
    """Install and kickstart the one launchd owner; launchd keeps a healthy existing owner unchanged."""
    if not launchd_codex_broker_owner_supported():
        config.warn_once("[spendguard] dedicated codex broker owner requires launchd on this platform")
        return False
    try:
        path = install_codex_broker_owner_launchd()
        domain = LAUNCHD_GUI_DOMAIN_TEMPLATE % os.getuid()
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True,
                       timeout=CODEX_BROKER_OWNER_KICKSTART_TIMEOUT_S)
        result = run_declared_launchd_action(("kickstart",), CODEX_BROKER_OWNER_LAUNCHD_LABEL)
        return result.returncode == 0
    except Exception as exc:
        config.warn_once("[spendguard] dedicated codex broker owner could not be started: %s" % str(exc)[:160])
        return False


def run_codex_broker_owner_entrypoint():
    from . import fleet
    from .codex_daemon import run_dedicated_codex_broker_owner
    fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON,
                            supervision_mode=fleet.SUPERVISION_LAUNCHD,
                            supervision_ref=CODEX_BROKER_OWNER_LAUNCHD_LABEL)
    return 0 if run_dedicated_codex_broker_owner() else 1


if __name__ == "__main__":
    raise SystemExit(run_codex_broker_owner_entrypoint())
