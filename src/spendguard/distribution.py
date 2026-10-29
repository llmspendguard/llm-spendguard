"""Fleet convergence and fail-closed release attestation (no model calls)."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import contextlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, consumer_audit, fleet, release, setup
from ._version import __version__ as SPENDGUARD_WRITER_VERSION
from .distribution_daemon import attest_distribution_daemons

DISTRIBUTION_CONFIG_SECTION = "distribute"
DISTRIBUTION_PROBE_TIMEOUT_S = consumer_audit.CONSUMER_AUDIT_PROBE_TIMEOUT_SECONDS
DISTRIBUTION_INSTALL_TIMEOUT_S = fleet._CONSUMER_EDITABLE_REFRESH_TIMEOUT_S
DISTRIBUTION_CHECKOUT_DIRECTORY = "distribution-checkouts"
DISTRIBUTION_VERSION_SYMBOL = "__version__"
DISTRIBUTION_WHEELS_DIRECTORY = "distribution-wheels"
DISTRIBUTION_EPHEMERAL_MARKER = ".spendguard-ephemeral"
DISTRIBUTION_BUILD_TIMEOUT_SETTING = "install_timeout_s"
DISTRIBUTION_MAX_WORKERS_SETTING = "max_install_workers"
DISTRIBUTION_DEFAULT_MAX_WORKERS = 4
DISTRIBUTION_WHEEL_BUILD_COMMAND = ("-m", "pip", "wheel", "--no-deps", "--no-build-isolation")
DISTRIBUTION_GIT_METADATA_NAME = ".git"
DISTRIBUTION_GITDIR_PREFIX = "gitdir:"
DISTRIBUTION_WORKTREES_DIRECTORY = "worktrees"
DISTRIBUTION_SNAPSHOT_VANISHED_ENTRY_MESSAGE = "distribution snapshot omitted vanished source entry: %s"
DISTRIBUTION_PROBE_UNRESOLVABLE_OWNER_MESSAGE = "distribution probe root has unresolvable owner marker: %s"
DISTRIBUTION_WHEEL_ALREADY_CONVERGED_ACTION = "wheel-already-converged"
DISTRIBUTION_EDITABLE_ALREADY_CONVERGED_ACTION = "editable-already-converged"
DISTRIBUTION_SNAPSHOT_MISSING_ENTRY_SENTINEL = "missing-after-walk"
DISTRIBUTION_SNAPSHOT_NONREGULAR_ENTRY_SENTINEL = "nonregular-entry"
DISTRIBUTION_SNAPSHOT_LOGGER = logging.getLogger(__name__)
DISTRIBUTION_PROBE_BASE_LOCK = threading.Lock()
DISTRIBUTION_PROBE_HOME_HASH_LENGTH = 16
DISTRIBUTION_PROBE_OWNER_PID_KEY = "owner_pid"
DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY = "owner_create_time"
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


def resolve_distribution_target(version=None):
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
    return {"sha": pointer["sha"], "version": version or versions[0], "green_version": versions[0],
            "checkout": str(repository)}


def distribution_wheel_cache_path():
    """Return the configured-home wheel cache shared by convergence runs."""
    return Path(config.HOME) / DISTRIBUTION_WHEELS_DIRECTORY


def mark_distribution_ephemeral_dir(path):
    """Create a spendguard-owned disposable directory and record that ownership."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / DISTRIBUTION_EPHEMERAL_MARKER).touch(exist_ok=True)
    return directory


def cached_distribution_wheels(version):
    """Return cached wheels whose parsed distribution and version match exactly."""
    from .manifest import MANIFEST_DISTRIBUTION_NAME
    try:
        from packaging.utils import parse_wheel_filename
    except ModuleNotFoundError:
        from pip._vendor.packaging.utils import parse_wheel_filename
    matches = []
    cache = distribution_wheel_cache_path()
    for candidate in cache.glob("*.whl") if cache.is_dir() else ():
        try:
            name, wheel_version, _build, _tags = parse_wheel_filename(candidate.name)
        except ValueError:
            continue
        if str(name) == MANIFEST_DISTRIBUTION_NAME and str(wheel_version) == version:
            matches.append(candidate)
    return sorted(matches)


def ensure_distribution_wheel(target, checkout, settings):
    """Reuse or build the green wheel, then verify its embedded filename version."""
    cache = mark_distribution_ephemeral_dir(distribution_wheel_cache_path())
    existing = cached_distribution_wheels(target["version"])
    if existing:
        return existing[0]
    if target["version"] != target.get("green_version", target["version"]):
        raise FileNotFoundError(f"rollback wheel is not cached: {target['version']}")
    command = [sys.executable, *DISTRIBUTION_WHEEL_BUILD_COMMAND, "--wheel-dir", str(cache), str(checkout)]
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=settings[DISTRIBUTION_BUILD_TIMEOUT_SETTING], check=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "wheel build failed").strip())
    built = cached_distribution_wheels(target["version"])
    if not built:
        raise ValueError(f"built wheel version does not match target: {target['version']}")
    return built[0]


def distribution_clean_environment():
    """Prevent the caller's PYTHONPATH from disguising the target installation."""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


def distribution_config_value(key):
    """Read a distribution knob using its declarative schema default as the sole default."""
    from .config_schema import SETTINGS
    entries = [entry for entry in SETTINGS if entry["section"] == DISTRIBUTION_CONFIG_SECTION
               and entry["key"] == key]
    if len(entries) != 1:
        raise KeyError(f"distribution setting is not uniquely registered: {key}")
    return config._cfg_get(DISTRIBUTION_CONFIG_SECTION, key, entries[0]["default"])


def distribution_probe_cache_paths():
    resolved_home = str(Path(config.HOME).expanduser().resolve(strict=False))
    home_namespace = hashlib.sha256(resolved_home.encode()).hexdigest()[:DISTRIBUTION_PROBE_HOME_HASH_LENGTH]
    cache = Path(str(distribution_config_value("probe_cache_root"))).expanduser() / home_namespace
    return (cache, cache / str(distribution_config_value("probe_cache_base_name")),
            cache / str(distribution_config_value("probe_cache_record_name")))


def distribution_configured_ledger_paths():
    """Resolve effective ledger identities and exact SQLite sidecar identities."""
    resolved_paths = set()
    for configured_path in (config.db_path(), config.lane_queue_db_path()):
        ledger_path = Path(configured_path).expanduser().resolve()
        resolved_paths.update({ledger_path, *(Path(str(ledger_path) + suffix)
                                              for suffix in ("-wal", "-shm", "-journal"))})
    return resolved_paths


def distribution_snapshot_excluded(directory, name):
    """Exclude only top-level entries whose spendguard ownership is proven."""
    source_home = Path(config.HOME).resolve(strict=False)
    parent = Path(directory).resolve(strict=False)
    if parent != source_home:
        return False
    entry = (parent / name).resolve(strict=False)
    if entry in distribution_configured_ledger_paths():
        return True
    if entry.is_dir() and (entry / DISTRIBUTION_EPHEMERAL_MARKER).exists():
        return True
    configured_probe_cache = (source_home / str(
        distribution_config_value("probe_cache_dirname"))).resolve(strict=False)
    return entry == configured_probe_cache and entry.is_dir()


def distribution_snapshot_fingerprint(source):
    """Hash the complete post-exclusion file-state manifest without reading file contents."""
    manifest_rows = []
    if not source.exists():
        return hashlib.sha256(b"").hexdigest()
    for directory, names, files in os.walk(source):
        names[:] = sorted(name for name in names
                          if not distribution_snapshot_excluded(directory, name))
        for name in sorted(files):
            path = Path(directory) / name
            if distribution_snapshot_excluded(directory, name):
                continue
            try:
                mode = path.stat().st_mode
                if not stat.S_ISREG(mode):
                    manifest_rows.append((path.relative_to(source).as_posix(),
                                          DISTRIBUTION_SNAPSHOT_NONREGULAR_ENTRY_SENTINEL,
                                          stat.S_IFMT(mode)))
                    continue
                details = path.stat()
            except OSError:
                manifest_rows.append((path.relative_to(source).as_posix(),
                                      DISTRIBUTION_SNAPSHOT_MISSING_ENTRY_SENTINEL,
                                      DISTRIBUTION_SNAPSHOT_MISSING_ENTRY_SENTINEL))
                continue
            manifest_rows.append((path.relative_to(source).as_posix(), details.st_size,
                                  details.st_mtime_ns))
    manifest_rows.sort(key=lambda row: row[0])
    encoded = json.dumps(manifest_rows, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def rebuild_or_reuse_distribution_probe_base():
    """Reuse an unchanged cached policy snapshot or atomically rebuild it once."""
    source = Path(config.HOME)
    cache, base, record = distribution_probe_cache_paths()
    mark_distribution_ephemeral_dir(cache)
    sweep_stale_distribution_probe_base_builds(cache, base)
    fingerprint = distribution_snapshot_fingerprint(source)
    try:
        previous = json.loads(record.read_text())
    except (OSError, ValueError, TypeError):
        previous = {}
    if base.is_dir() and previous.get("fingerprint") == fingerprint:
        mark_distribution_ephemeral_dir(base)
        return base
    build = cache / f"{base.name}.building-{os.getpid()}"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir()
    write_distribution_probe_owner_marker(
        build / str(distribution_config_value("probe_temp_marker_name")))
    if source.exists():
        shutil.copytree(source, build, ignore=distribution_snapshot_ignored_entries,
                        copy_function=copy_distribution_snapshot_entry, dirs_exist_ok=True)
    mark_distribution_ephemeral_dir(build)
    if base.exists():
        shutil.rmtree(base)
    build.replace(base)
    if config.update_json(record, lambda _previous: {"fingerprint": fingerprint}) is None:
        raise OSError("could not persist distribution probe snapshot fingerprint")
    return base


def ensure_distribution_probe_base():
    """Serialize cached-base validation because deploy probes can run in worker threads."""
    with DISTRIBUTION_PROBE_BASE_LOCK:
        return rebuild_or_reuse_distribution_probe_base()


def sweep_stale_distribution_probe_roots():
    """Remove marked probe roots only when their recorded owner is provably gone."""
    temporary_root = Path(tempfile.gettempdir())
    prefix = str(distribution_config_value("probe_temp_prefix"))
    marker = str(distribution_config_value("probe_temp_marker_name"))
    sweep_distribution_probe_owner_candidates(temporary_root.glob(f"{prefix}*"), marker)


def sweep_stale_distribution_probe_base_builds(cache, base):
    """Reclaim interrupted cached-base builds only after proving the owner is gone."""
    marker = str(distribution_config_value("probe_temp_marker_name"))
    sweep_distribution_probe_owner_candidates(cache.glob(f"{base.name}.building-*"), marker)


def sweep_distribution_probe_owner_candidates(candidates, marker):
    """Apply the recycle-safe owner proof uniformly to marked probe directories."""
    for candidate in candidates:
        marker_path = candidate / marker
        if not candidate.is_dir() or not marker_path.is_file():
            continue
        try:
            owner = json.loads(marker_path.read_text())
            owner_pid = int(owner[DISTRIBUTION_PROBE_OWNER_PID_KEY])
            owner_create_time = owner[DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            DISTRIBUTION_SNAPSHOT_LOGGER.warning(
                DISTRIBUTION_PROBE_UNRESOLVABLE_OWNER_MESSAGE, candidate)
            continue
        if owner_pid == os.getpid():
            continue
        identity = fleet._classify_fleet_pid_identity(owner_pid, owner_create_time)
        if identity == fleet._FLEET_PID_GONE:
            shutil.rmtree(candidate, ignore_errors=True)


def write_distribution_probe_owner_marker(marker_path):
    """Record a recycle-safe process identity before exposing a sweepable probe root."""
    owner_pid = os.getpid()
    owner_create_time = repr(fleet.psutil.Process(owner_pid).create_time())
    marker = {DISTRIBUTION_PROBE_OWNER_PID_KEY: owner_pid,
              DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY: owner_create_time}
    if config.update_json(marker_path, lambda _previous: marker) is None:
        raise OSError("could not persist distribution probe owner marker")


def clone_distribution_probe_base(base, destination, timeout_seconds):
    """Create a writable clone without hardlinks; fall back to a full independent copy."""
    try:
        clone = subprocess.run(["cp", "-c", "-R", str(base), str(destination)],
                               capture_output=True, check=False, timeout=timeout_seconds)
        cloned = clone.returncode == 0 and destination.is_dir()
    except (OSError, subprocess.SubprocessError):
        cloned = False
    if not cloned:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(base, destination, copy_function=copy_distribution_snapshot_entry)


@contextlib.contextmanager
def distribution_probe_environment(timeout_seconds=None):
    """Observe the real policy through a disposable, complete state snapshot.

    Startup hooks and doctor may register an interpreter or initialize a ledger.
    Keeping all state, including gate-disable markers, preserves their proof while
    confining those incidental writes to temporary storage for every probe.
    """
    sweep_stale_distribution_probe_roots()
    base = ensure_distribution_probe_base()
    prefix = str(distribution_config_value("probe_temp_prefix"))
    marker = str(distribution_config_value("probe_temp_marker_name"))
    with tempfile.TemporaryDirectory(prefix=prefix) as probe_state_directory:
        root = Path(probe_state_directory)
        write_distribution_probe_owner_marker(root / marker)
        snapshot = root / Path(config.HOME).name
        clone_timeout = (distribution_policy()["probe_timeout_s"]
                         if timeout_seconds is None else timeout_seconds)
        clone_distribution_probe_base(base, snapshot, clone_timeout)
        distribution_snapshot_configured_ledgers(snapshot)
        environment = distribution_clean_environment()
        environment["SPENDGUARD_HOME"] = str(snapshot)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        yield environment


def copy_distribution_snapshot_entry(source, destination):
    """Copy stable probe state while tolerating an entry proven to have vanished."""
    try:
        return shutil.copy2(source, destination)
    except FileNotFoundError:
        if Path(source).exists():
            raise
        DISTRIBUTION_SNAPSHOT_LOGGER.warning(DISTRIBUTION_SNAPSHOT_VANISHED_ENTRY_MESSAGE, source)
        return str(destination)


def distribution_snapshot_runtime_endpoints(directory, names):
    """Runtime sockets/FIFOs/devices carry no stored policy and cannot be copied."""
    return [name for name in names
            if not (stat.S_ISREG((Path(directory) / name).stat().st_mode)
                    or stat.S_ISDIR((Path(directory) / name).stat().st_mode))]


def distribution_snapshot_ignored_entries(directory, names):
    """Compose runtime endpoint removal with configured recovery/output exclusions."""
    ignored = set(distribution_snapshot_runtime_endpoints(directory, names))
    ignored.update(name for name in names if distribution_snapshot_excluded(directory, name))
    return sorted(ignored)


def distribution_manifest_age_seconds(persisted_manifest, now=None):
    verified = datetime.fromisoformat(persisted_manifest["last_verified_at"])
    current = now or datetime.now(timezone.utc)
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    return max(0.0, (current - verified).total_seconds())


def distribution_manifest_cache_status(persisted_manifest, roots, audit_settings, now=None):
    """Validate writer identity, TTL, and scan-root state before serving enumeration."""
    from . import manifest
    writer = persisted_manifest.get("writer_version")
    status = {"writer_version": writer, "timestamp": persisted_manifest.get("last_verified_at"),
              "authoritative": False, "reason": None}
    if writer != SPENDGUARD_WRITER_VERSION:
        status["reason"] = "writer version mismatch; stale, re-enumerate"
        return status
    try:
        ttl = float(distribution_config_value("enumeration_cache_ttl_s"))
        if distribution_manifest_age_seconds(persisted_manifest, now=now) > ttl:
            status["reason"] = "enumeration cache TTL expired; stale, re-enumerate"
            return status
    except (KeyError, TypeError, ValueError):
        status["reason"] = "manifest timestamp invalid; stale, re-enumerate"
        return status
    configured_roots = audit_settings.get("workspace_roots", ()) if roots is None else roots
    current_mtimes = manifest.manifest_scan_root_mtimes(configured_roots)
    if persisted_manifest.get("enumeration_root_mtimes") != current_mtimes:
        status["reason"] = "enumeration roots changed; stale, re-enumerate"
        return status
    status["authoritative"] = True
    return status


def distribution_manifest_install_cache_valid(surface, now=None):
    """Validate the independent short-lived install audit key."""
    from . import manifest
    try:
        ttl = float(distribution_config_value("audit_cache_ttl_s"))
        if distribution_manifest_age_seconds(surface, now=now) > ttl:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return surface.get("audit_cache_key") == manifest.interpreter_audit_cache_key(surface["canonical_path"])


def distribution_snapshot_configured_ledgers(snapshot):
    """Clone explicit SQLite targets and their complete WAL before observing them."""
    policy_path = snapshot / config.CONFIG_JSON.name
    if not policy_path.exists():
        return
    policy = json.loads(policy_path.read_text())
    budget_policy = policy.get("budget", {})
    configured_ledger_paths = distribution_configured_ledger_paths()
    for setting in ("db_path", "lane_queue_db_path"):
        configured_path = budget_policy.get(setting)
        if not configured_path:
            continue
        ledger_source = Path(configured_path).expanduser().resolve()
        if ledger_source not in configured_ledger_paths:
            raise ValueError(f"configured ledger identity was not resolved: {setting}")
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
    if not configured:
        mark_distribution_ephemeral_dir(destination.parent)
    if not destination.exists():
        if configured:
            destination.parent.mkdir(parents=True, exist_ok=True)
        distribution_git_query(checkout, "worktree", "add", "--detach", str(destination), target["sha"])
    if (distribution_git_query(destination, "rev-parse", "HEAD") != target["sha"]
            or distribution_git_query(destination, "status", "--porcelain", "--untracked-files=no")):
        raise ValueError(f"deployed checkout is not clean at target SHA: {destination}")
    return str(destination)


def probe_distribution_interpreter(python, settings):
    try:
        timeout_seconds = settings.get("probe_timeout_s", DISTRIBUTION_PROBE_TIMEOUT_S)
        with distribution_probe_environment(timeout_seconds) as probe_environment:
            completed = subprocess.run([str(python), "-c", DISTRIBUTION_INTERPRETER_PROBE],
                                       env=probe_environment, capture_output=True, text=True,
                                       timeout=timeout_seconds, check=False)
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


def distribution_wheel_interpreter_on_target(probe, target):
    return bool(probe.get("confirmed") and probe.get("version") == target["version"]
                and probe.get("metadata_version") == target["version"])


def distribution_install_attestation(install, probe, target, *, dev_venv=False):
    """Return install-kind-aware target status and a stable machine reason."""
    from . import manifest
    kind = install.get("install_kind")
    if dev_venv:
        dev_target = {**target, "version": target.get("green_version", target["version"])}
        on_target = kind == manifest.MANIFEST_EDITABLE_KIND and distribution_interpreter_on_target(probe, dev_target)
        return on_target, None if on_target else "dev_editable_not_green"
    if kind == manifest.MANIFEST_EDITABLE_KIND:
        return False, "still_editable"
    if kind != manifest.MANIFEST_WHEEL_KIND:
        return False, "wheel_missing"
    if install.get("wheel_version") != target["version"]:
        return False, "wrong_version"
    if not distribution_wheel_interpreter_on_target(probe, target):
        return False, "identity_not_green"
    return True, None


def distribution_common_git_directory(working_tree):
    """Resolve a working tree's shared git directory from directory or gitfile metadata."""
    git_metadata = working_tree / DISTRIBUTION_GIT_METADATA_NAME
    try:
        if git_metadata.is_dir():
            return git_metadata.resolve(), None
        if not git_metadata.is_file():
            return None, "git_metadata_unknown"
        marker = git_metadata.read_text().strip()
        if not marker.startswith(DISTRIBUTION_GITDIR_PREFIX):
            return None, "git_metadata_unknown"
        git_directory = Path(marker[len(DISTRIBUTION_GITDIR_PREFIX):].strip()).resolve()
        common_git_directory = (
            git_directory.parents[1]
            if git_directory.parent.name == DISTRIBUTION_WORKTREES_DIRECTORY
            else git_directory
        )
        return common_git_directory, None
    except (OSError, IndexError):
        return None, "git_metadata_unknown"


def distribution_enclosing_working_tree(venv_path):
    """Find the nearest resolved ancestor carrying git working-tree metadata."""
    try:
        resolved_venv_path = Path(venv_path).resolve()
        for candidate in (resolved_venv_path, *resolved_venv_path.parents):
            git_metadata = candidate / DISTRIBUTION_GIT_METADATA_NAME
            if git_metadata.is_dir() or git_metadata.is_file():
                return candidate, None
        return None, None
    except OSError:
        return None, "venv_working_tree_unknown"


def distribution_dev_venv_status(install, target, venv_path):
    """Classify dev from the venv's own membership in this repository's working trees."""
    from . import manifest
    if install.get("install_kind") != manifest.MANIFEST_EDITABLE_KIND:
        return False, None
    canonical_checkout = Path(target.get("checkout", Path(__file__).resolve().parents[2])).resolve()
    enclosing_working_tree, working_tree_error = distribution_enclosing_working_tree(venv_path)
    if working_tree_error:
        return None, working_tree_error
    if enclosing_working_tree is None:
        return False, None
    if enclosing_working_tree == canonical_checkout:
        return True, None
    canonical_common_git, canonical_git_error = distribution_common_git_directory(canonical_checkout)
    if canonical_git_error:
        return None, "canonical_source_unknown"
    venv_common_git, venv_git_error = distribution_common_git_directory(enclosing_working_tree)
    if venv_git_error:
        return None, "venv_working_tree_unknown"
    return venv_common_git == canonical_common_git, None


def inspect_distribution_install(python, policy):
    """Inspect installation metadata without allowing startup hooks to mutate live state."""
    from . import manifest
    timeout = policy.get("probe_timeout_s", DISTRIBUTION_PROBE_TIMEOUT_S)
    with distribution_probe_environment(timeout) as environment:
        return manifest.inspect_interpreter_install_kind(
            python, timeout_seconds=timeout, environment=environment)


def distribution_surface_row(surface, kind, on_version=False, healthy=False, fd_ok=None, action="none", reason=None, **evidence):
    return {"surface": str(surface), "kind": kind, "on_version": bool(on_version), "healthy": bool(healthy),
            "fd_ok": fd_ok, "action_taken": action,
            "straggler_reason": reason or (None if on_version and healthy and fd_ok is not False else "identity or health unproven"),
            **evidence}


def attest_distribution_host(target, settings):
    probe = probe_distribution_interpreter(sys.executable, settings)
    try:
        timeout_seconds = settings.get("probe_timeout_s", DISTRIBUTION_PROBE_TIMEOUT_S)
        with distribution_probe_environment(timeout_seconds) as probe_environment:
            result = subprocess.run([sys.executable, "-m", "spendguard", "doctor"],
                                    env=probe_environment, capture_output=True, text=True,
                                    timeout=timeout_seconds, check=False)
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
            restart_ok = bool(restarted.get(fleet.FLEET_RESTART_REQUESTED_KEY))
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
        reason = "required API consumer restart could not be requested" if not restart_ok else None
        rows.append(distribution_surface_row(surface, "api", on_version, healthy, action=action, reason=reason,
                                             registration=candidate, probe=probe, restart=restarted))
    return rows


def distribute_fleet(*, check=False, roots=None, dry_run=False, settings=None, version=None):
    """Idempotent scheduler/library entry. Any missing proof is a named straggler."""
    rows = []
    target = None
    audit_pruning = []
    manifest_cache = None
    try:
        target = resolve_distribution_target(version=version)
        policy = distribution_policy()
        audit_settings = config.consumer_audit_settings() if settings is None else settings
        try:
            audit = consumer_audit.audit_consumer_workspaces(target["sha"], roots=roots, auto_arm=False,
                                                             dry_run=True, settings=audit_settings)
        except Exception as exc:
            rows.append(distribution_surface_row("consumer-audit", "enumeration", reason=str(exc)))
            audit = {"consumers": [], "pruned_at_depth": []}
        try:
            if check:
                from .manifest import manifest_path, read_surface_manifest
                persisted_manifest = read_surface_manifest() if manifest_path().is_file() else None
            else:
                persisted_manifest = None
            if persisted_manifest is not None:
                manifest_cache = distribution_manifest_cache_status(
                    persisted_manifest, roots, audit_settings)
            if persisted_manifest is not None and manifest_cache["authoritative"]:
                inventory = {
                    "interpreters": [
                        {"python": surface["canonical_path"], "venv": str(Path(surface["canonical_path"]).parent.parent),
                         "kind": "venv", **({"install": {
                             "install_kind": surface.get("install_kind"),
                             "wheel_version": surface.get("wheel_version"),
                             "editable_origin": surface.get("editable_origin"),
                             "verification_error": surface.get("verification_error"),
                         }} if distribution_manifest_install_cache_valid(surface) else {})}
                        for surface in persisted_manifest["surfaces"] if surface["surface_kind"] == "venv"
                    ],
                    "gaps": persisted_manifest.get("gaps", []),
                }
            else:
                inventory = setup.discover_distribution_interpreters(roots=roots, settings=audit_settings)
                if persisted_manifest is not None and manifest_cache and "writer version mismatch" in manifest_cache["reason"]:
                    rows.append(distribution_surface_row(
                        "surface-manifest", "enumeration", reason=manifest_cache["reason"]))
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
        wheel_error = None
        if not check and not dry_run:
            try:
                checkout = prepare_distribution_checkout(target, policy)
                ensure_distribution_wheel(target, checkout, policy)
            except Exception as exc:
                checkout_error = str(exc)
                wheel_error = f"wheel unavailable: {exc}"

        planned = []
        for python, item in sorted(interpreters.items()):
            install = item.get("install") or inspect_distribution_install(python, policy)
            managed_venv = item.get("kind") == "venv"
            dev_status, classification_error = (distribution_dev_venv_status(
                install, target, item.get("venv") or python)
                                                 if managed_venv else (True, None))
            planned.append((python, item, install, dev_status, classification_error))

        install_results = {}
        wheel_candidates = [(python, item) for python, item, _install, dev_status, classification_error in planned
                            if dev_status is False and classification_error is None]
        if not check and not dry_run and checkout and not wheel_error:
            configured_workers = int(config._cfg_get(
                DISTRIBUTION_CONFIG_SECTION, DISTRIBUTION_MAX_WORKERS_SETTING,
                DISTRIBUTION_DEFAULT_MAX_WORKERS))
            worker_count = max(1, min(configured_workers, len(wheel_candidates) or 1))
            def install_distribution_wheel_after_live_probe(python, item):
                probe = probe_distribution_interpreter(python, policy)
                action = DISTRIBUTION_WHEEL_ALREADY_CONVERGED_ACTION
                if not distribution_wheel_interpreter_on_target(probe, target):
                    setup.refresh_consumer_wheel_package(
                        python, distribution_wheel_cache_path(), target["version"], policy["install_timeout_s"])
                    action = "wheel-installed"
                hook_code = (setup.install_hook(item["venv"], install_pkg=False) if item.get("venv")
                             else setup.install_hook(user=True, python=python, install_pkg=False))
                return action, hook_code

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {executor.submit(
                    install_distribution_wheel_after_live_probe, python, item): (python, item)
                    for python, item in wheel_candidates}
                for future in as_completed(futures):
                    python, item = futures[future]
                    try:
                        action, hook_code = future.result()
                        install_results[python] = (action, None if not hook_code else f"gate arm exited {hook_code}")
                    except Exception as exc:
                        install_results[python] = ("wheel-install-failed", f"convergence failed: {exc}")

        for python, item, install, dev_status, classification_error in planned:
            action = "none"
            error = classification_error
            if dev_status is True and not check and not dry_run and checkout:
                try:
                    live_probe = probe_distribution_interpreter(python, policy)
                    if distribution_interpreter_on_target(live_probe, target):
                        action = DISTRIBUTION_EDITABLE_ALREADY_CONVERGED_ACTION
                    else:
                        fleet._refresh_editable_install(python, checkout, policy["install_timeout_s"])
                        action = "editable-reinstalled"
                    hook_code = (setup.install_hook(item["venv"], install_pkg=False) if item.get("venv")
                                 else setup.install_hook(user=True, python=python, install_pkg=False))
                    if hook_code:
                        error = f"gate arm exited {hook_code}"
                except Exception as exc:
                    error = f"convergence failed: {exc}"
            elif dry_run and not check:
                action = "would-reinstall-editable" if dev_status is True else "would-install-wheel"
            elif not check and not dry_run and dev_status is False:
                action, error = install_results.get(python, ("wheel-install-failed", wheel_error or checkout_error))
            probe = probe_distribution_interpreter(python, policy)
            refreshed_install = (inspect_distribution_install(python, policy)
                if not check and not dry_run and error is None else install)
            if item.get("kind") == "venv":
                on_target, attestation_reason = distribution_install_attestation(
                    refreshed_install, probe, target, dev_venv=dev_status is True)
            else:
                on_target = distribution_interpreter_on_target(probe, target)
                attestation_reason = None if on_target else "identity_not_green"
            rows.append(distribution_surface_row(python, item["kind"], on_target,
                                                 probe.get("confirmed") and probe.get("gate_enforcing") and not error,
                                                 action=action, reason=error or attestation_reason, probe=probe,
                                                 install=refreshed_install, dev_venv=dev_status is True))
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
            "manifest_cache": manifest_cache,
            "enumeration": {"audit_pruned_at_depth": audit_pruning},
            "summary": {"converged": len(rows) - len(stragglers), "total": len(rows), "stragglers": stragglers},
            "exit_code": int(bool(stragglers) or not rows)}


def render_distribution_table(result):
    fields = ("surface", "kind", "on_version", "healthy", "fd_ok", "action_taken", "straggler_reason")
    cache = result.get("manifest_cache")
    lines = (["manifest/cache writer-version={} timestamp={} authoritative={}{}".format(
        cache.get("writer_version"), cache.get("timestamp"), cache.get("authoritative"),
        f" reason={cache.get('reason')}" if cache.get("reason") else "")] if cache else [])
    lines.append(" | ".join(fields))
    lines.extend(" | ".join(str(row[field]) for field in fields) for row in result["surfaces"])
    lines.append(json.dumps({"summary": result["summary"], "exit_code": result["exit_code"]}, sort_keys=True))
    return "\n".join(lines)


def parse_distribution_cli_arguments(argv=None):
    """Share the command grammar with startup's verify-only dispatch."""
    parser = argparse.ArgumentParser(prog="spendguard distribute")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--roots", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version")
    return parser.parse_args(argv)


def distribute_command(argv=None):
    args = parse_distribution_cli_arguments(argv)
    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            result = distribute_fleet(check=args.check, roots=args.roots, dry_run=args.dry_run, version=args.version)
    else:
        result = distribute_fleet(check=args.check, roots=args.roots, dry_run=args.dry_run, version=args.version)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_distribution_table(result))
    return result["exit_code"]
