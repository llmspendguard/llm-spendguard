"""Persisted inventory of every locally discovered spendguard surface."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess

from . import config, fleet, setup

MANIFEST_FILENAME = "surface-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_PROBE_TIMEOUT_SECONDS = 15.0
MANIFEST_DISTRIBUTION_NAME = "llm-spendguard"
MANIFEST_IMPORT_PACKAGE_NAME = "spendguard"
MANIFEST_CLI_EXECUTABLE_NAME = "spendguard"
MANIFEST_EDITABLE_KIND = "editable"
MANIFEST_WHEEL_KIND = "wheel"
MANIFEST_UNKNOWN_KIND = "unknown"
MANIFEST_DAEMON_CONSUMER_ROLE = fleet.CONSUMER_ROLE_DAEMON
MANIFEST_DAEMON_SURFACE_KIND = "mcp"
MANIFEST_RUNNING_CONSUMER_SURFACE_KIND = "consumer"
MANIFEST_UNREPRESENTABLE_CONSUMER_REASON = "missing_executable_and_supervision_identity"
MANIFEST_INTERPRETER_PROBE = r'''import importlib.metadata,importlib.util,json,pathlib,sys,urllib.parse
name,package=sys.argv[1:3]
dist=importlib.metadata.distribution(name)
direct=dist.read_text('direct_url.json')
kind=None
reason=None
editable_origin=None
if direct:
 try:
  data=json.loads(direct)
  if data.get('dir_info',{}).get('editable') is True:
   kind='editable'
   parsed=urllib.parse.urlparse(data.get('url',''))
   if parsed.scheme=='file': editable_origin=str(pathlib.Path(urllib.parse.unquote(parsed.path)).resolve())
  elif data.get('url'):
   parsed=urllib.parse.urlparse(data['url'])
   if parsed.scheme=='file' or parsed.path.lower().endswith('.whl') or data.get('archive_info') is not None: kind='wheel'
 except (TypeError,ValueError,json.JSONDecodeError): reason='invalid direct_url.json'
metadata_root=pathlib.Path(getattr(dist,'_path','')).resolve()
site_root=metadata_root.parent
normalized=name.lower().replace('-','_').replace('.','_')
if kind is None and metadata_root:
 markers=tuple(site_root.glob('__editable__.'+normalized+'*.pth'))+tuple(site_root.glob('__editable___'+normalized+'*finder.py'))+tuple(site_root.glob(normalized+'*.egg-link'))
 if markers: kind='editable'
if kind is None and metadata_root:
 spec=importlib.util.find_spec(package)
 origin=pathlib.Path(spec.origin).resolve() if spec and spec.origin else None
 if origin:
  if origin.is_relative_to(site_root): kind='wheel'
  else: kind='editable'
if kind is None: reason=reason or 'distribution metadata and import origin did not resolve install kind'
print(json.dumps({'install_kind':kind or 'unknown','wheel_version':dist.version if kind=='wheel' else None,
 'editable_origin':editable_origin,**({'verification_error':reason} if reason else {})}))
'''


def manifest_path() -> Path:
    """Return the configured manifest location."""
    return Path(config.HOME) / MANIFEST_FILENAME


def canonical_manifest_surface_path(value: str | os.PathLike[str]) -> str:
    """Canonicalize an existing or declared surface without requiring it to exist."""
    return str(Path(value).expanduser().resolve(strict=False))


def inspect_interpreter_install_kind(python_executable: str, *, timeout_seconds: float, environment=None) -> dict:
    """Read installed distribution metadata in the target interpreter."""
    try:
        completed = subprocess.run(
            [python_executable, "-c", MANIFEST_INTERPRETER_PROBE, MANIFEST_DISTRIBUTION_NAME,
             MANIFEST_IMPORT_PACKAGE_NAME],
            capture_output=True, text=True, timeout=timeout_seconds, check=False, env=environment,
        )
        if completed.returncode:
            return {"install_kind": MANIFEST_UNKNOWN_KIND, "wheel_version": None,
                    "verification_error": completed.stderr or completed.stdout}
        result = json.loads(completed.stdout)
        if result.get("install_kind") not in (
                MANIFEST_EDITABLE_KIND, MANIFEST_WHEEL_KIND, MANIFEST_UNKNOWN_KIND):
            raise ValueError("interpreter returned an invalid install kind")
        return result
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        return {"install_kind": MANIFEST_UNKNOWN_KIND, "wheel_version": None,
                "verification_error": str(exc)}


def _manifest_served_identity(python_executable: str, settings: dict) -> dict:
    """Resolve served identity through distribution's existing isolated probe."""
    from .distribution import probe_distribution_interpreter
    proof = probe_distribution_interpreter(python_executable, settings)
    return {"served_sha": proof.get("sha"), "role": "interpreter",
            **({"verification_error": proof.get("error")} if proof.get("error") else {})}


def build_surface_manifest(*, roots=None, settings=None, now=None) -> dict:
    """Eagerly discover, verify, and atomically persist the complete bounded inventory."""
    resolved_settings = config.consumer_audit_settings() if settings is None else settings
    verified_at = (now or datetime.now(timezone.utc)).isoformat()
    timeout_seconds = float(resolved_settings.get(
        "distribution_probe_timeout_s",
        config._cfg_get("distribute", "probe_timeout_s", MANIFEST_PROBE_TIMEOUT_SECONDS),
    ))
    discovery = setup.discover_distribution_interpreters(roots=roots, settings=resolved_settings)
    surfaces: dict[tuple[str, str], dict] = {}
    interpreter_installs: dict[str, dict] = {}
    for item in discovery["interpreters"]:
        python_executable = canonical_manifest_surface_path(item["python"])
        install = inspect_interpreter_install_kind(python_executable, timeout_seconds=timeout_seconds)
        interpreter_installs[python_executable] = install
        identity = _manifest_served_identity(python_executable, {"probe_timeout_s": timeout_seconds})
        surfaces[("venv", python_executable)] = {
            "canonical_path": python_executable, "surface_kind": "venv",
            **install, **identity, "last_verified_at": verified_at,
        }

    for consumer in fleet.running_consumers(prune=False):
        role = consumer.get("role")
        executable = consumer.get("python_executable")
        supervision_identity = consumer.get("supervision_ref")
        if not executable and not supervision_identity:
            discovery["gaps"].append({
                "pid": consumer.get("pid"),
                "role": role,
                "reason": MANIFEST_UNREPRESENTABLE_CONSUMER_REASON,
            })
            continue
        surface_path = canonical_manifest_surface_path(executable) if executable else f"pid:{consumer['pid']}"
        surface_identity = supervision_identity or f"pid:{consumer['pid']}"
        surface_kind = (MANIFEST_DAEMON_SURFACE_KIND
                        if role == MANIFEST_DAEMON_CONSUMER_ROLE
                        else MANIFEST_RUNNING_CONSUMER_SURFACE_KIND)
        install = interpreter_installs.get(surface_path, {
            "install_kind": MANIFEST_UNKNOWN_KIND, "wheel_version": None,
        })
        surfaces[(surface_kind, surface_identity)] = {
            "canonical_path": surface_path, "surface_kind": surface_kind,
            **install,
            "served_sha": consumer.get("served_sha"), "role": role,
            "last_verified_at": verified_at,
        }

    cli_executable = shutil.which(MANIFEST_CLI_EXECUTABLE_NAME)
    if cli_executable:
        cli_path = canonical_manifest_surface_path(cli_executable)
        surfaces[("cli", cli_path)] = {
            "canonical_path": cli_path, "surface_kind": "cli",
            "install_kind": MANIFEST_UNKNOWN_KIND, "wheel_version": None,
            "served_sha": None, "role": "cli", "last_verified_at": verified_at,
        }

    manifest = {"schema_version": MANIFEST_SCHEMA_VERSION, "last_verified_at": verified_at,
                "surfaces": sorted(surfaces.values(), key=lambda row: (row["surface_kind"], row["canonical_path"])),
                "gaps": discovery["gaps"]}
    destination = manifest_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if config.update_json(destination, lambda _previous: manifest) is None:
        raise OSError(f"could not persist surface manifest: {destination}")
    return manifest


def read_surface_manifest() -> dict:
    """Read and validate the persisted manifest used as distribution's SSOT."""
    manifest = json.loads(manifest_path().read_text())
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or not isinstance(manifest.get("surfaces"), list):
        raise ValueError("surface manifest schema is invalid")
    return manifest


def manifest_command(argv=None) -> int:
    """Build or show the persisted complete surface inventory."""
    parser = argparse.ArgumentParser(prog="spendguard manifest")
    parser.add_argument("action", nargs="?", choices=("build", "show"), default="build")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--roots", nargs="+")
    arguments = parser.parse_args(argv)
    result = build_surface_manifest(roots=arguments.roots) if arguments.action == "build" else read_surface_manifest()
    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for surface in result["surfaces"]:
            print(" | ".join(str(surface.get(field)) for field in
                             ("surface_kind", "canonical_path", "install_kind", "wheel_version", "served_sha", "role")))
    return int(bool(result.get("gaps")))
