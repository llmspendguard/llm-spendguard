#!/usr/bin/env python3
"""Refresh every local venv that already imports this spendguard checkout.

Discovery is deliberately conservative: pinned/separate installs are reported but
never converted to editable installs, and every subprocess has its own timeout.
"""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tomllib
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_SRC = (REPO_ROOT / "src").resolve()
try:
    EXPECTED_VERSION = metadata.version("llm-spendguard")
except metadata.PackageNotFoundError:
    with (REPO_ROOT / "pyproject.toml").open("rb") as project_metadata:
        EXPECTED_VERSION = tomllib.load(project_metadata)["project"]["version"]
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
IMPORT_TIMEOUT = 10
DOCTOR_TIMEOUT = 60
PIP_TIMEOUT = 300
MAX_DEPTH = 3


def _short_error(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:limit]


def _run_venv_refresh_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _failure_output(result: subprocess.CompletedProcess[str], limit: int = 500) -> str:
    text = " ".join((result.stderr or result.stdout or "command failed").split())
    return text[-limit:]


def _fleet_venvs() -> tuple[list[Path], str | None]:
    """Read registered venv roots via spendguard.fleet._fleet_db, as requested."""
    original_fleet_import_path = sys.path.copy()
    sys.path.insert(0, str(REPO_SRC))
    try:
        from spendguard.fleet import _fleet_db

        connection = _fleet_db()
        try:
            rows = connection.execute(
                "SELECT DISTINCT venv_root FROM consumers "
                "WHERE venv_root IS NOT NULL AND venv_root != ''"
            ).fetchall()
        finally:
            connection.close()
        return [Path(row[0]).expanduser() for row in rows], None
    except (ImportError, OSError, sqlite3.Error) as exc:
        return [], _short_error(exc)
    finally:
        sys.path[:] = original_fleet_import_path


def _venv_refresh_discovery_defaults() -> tuple[list[Path], list[Path], str | None]:
    home = Path.home()
    fleet, fleet_error = _fleet_venvs()
    scan_roots = [home / "Documents" / "claude"]
    exact_venvs = fleet + [
        home / "Documents" / "claude" / "lmm" / ".venv",
        home / "Documents" / "claude" / "honestreview" / ".venv.nosync",
    ]
    return scan_roots, exact_venvs, fleet_error


def _is_venv(path: Path) -> bool:
    return (path / "bin" / "python").is_file() and (path / "pyvenv.cfg").is_file()


def _discover_under(root: Path, max_depth: int, depth_pruned: list[Path]) -> Iterable[Path]:
    if _is_venv(root):
        yield root
        return
    try:
        root_depth = len(root.parts)
        for current, dirs, _files in os.walk(root, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            if _is_venv(current_path):
                yield current_path
                dirs[:] = []
                continue
            if depth >= max_depth:
                depth_pruned.extend(current_path / name for name in dirs)
                dirs[:] = []
    except OSError:
        return


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = os.path.abspath(str(path))
        unique.setdefault(key, path)
    return [unique[key] for key in sorted(unique)]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _import_location(venv: Path) -> tuple[str | None, str | None]:
    command = [
        str(venv / "bin" / "python"),
        "-c",
        "import spendguard, os; print(os.path.dirname(spendguard.__file__))",
    ]
    try:
        result = _run_venv_refresh_command(command, IMPORT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"spendguard import timed out after {IMPORT_TIMEOUT}s"
    except OSError as exc:
        return None, f"venv python failed: {_short_error(exc)}"
    if result.returncode:
        detail = (result.stderr or result.stdout or "spendguard is not importable").strip()
        return None, _short_error(RuntimeError(detail))
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None, "spendguard import returned no path"
    return lines[-1], None


def _expected_refresh_version_in_doctor_output(output: str) -> str | None:
    for line in output.splitlines():
        if "llm-spendguard" in line.lower() or "version" in line.lower():
            if EXPECTED_VERSION in line:
                return EXPECTED_VERSION
    return EXPECTED_VERSION if EXPECTED_VERSION in output else None


def _repair_and_verify_explicit_venv(venv: Path) -> dict[str, object]:
    row: dict[str, object] = {
        "venv": str(venv),
        "classification": "editable-shared-tree",
        "refreshed": False,
        "version": None,
        "enforcing": False,
    }
    install_command = [
        str(venv / "bin" / "python"), "-m", "pip", "install", "-e", str(REPO_ROOT)
    ]
    try:
        install = _run_venv_refresh_command(install_command, PIP_TIMEOUT)
    except subprocess.TimeoutExpired:
        row["failure"] = f"pip install timed out after {PIP_TIMEOUT}s"
        return row
    except OSError as exc:
        row["failure"] = f"pip install failed to start: {_short_error(exc)}"
        return row
    if install.returncode:
        # Retry every failed install once; dependency resolution remains enabled.
        row["install_retry"] = "--no-build-isolation"
        try:
            install = _run_venv_refresh_command(install_command[:4] + [row["install_retry"]] + install_command[4:], PIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            row["failure"] = f"pip retry timed out after {PIP_TIMEOUT}s"
            return row
        except OSError as exc:
            row["failure"] = f"pip retry failed to start: {_short_error(exc)}"
            return row
        if install.returncode:
            row["failure"] = _failure_output(install)
            return row
    row["refreshed"] = True
    try:
        from spendguard.fleet import _dependency_fingerprint

        fingerprint = _dependency_fingerprint(REPO_ROOT / "pyproject.toml")
        (venv / ".spendguard_dep_fingerprint").write_text(fingerprint + "\n")
    except (ImportError, OSError, ValueError) as exc:
        row["failure"] = f"dependency fingerprint marker: {_short_error(exc)}"
        return row

    try:
        doctor = _run_venv_refresh_command([str(venv / "bin" / "spendguard"), "doctor"], DOCTOR_TIMEOUT)
    except subprocess.TimeoutExpired:
        row["failure"] = f"doctor timed out after {DOCTOR_TIMEOUT}s"
        return row
    except OSError as exc:
        row["failure"] = f"doctor failed to start: {_short_error(exc)}"
        return row
    doctor_output = "\n".join(part for part in (doctor.stdout, doctor.stderr) if part)
    row["version"] = _expected_refresh_version_in_doctor_output(doctor_output)
    row["enforcing"] = any(
        "ENFORCING HERE:" in line and "YES" in line for line in doctor_output.splitlines()
    )
    row["doctor_exit"] = doctor.returncode

    imported_from, import_error = _import_location(venv)
    row["imports_from"] = imported_from
    if import_error:
        row["failure"] = f"post-refresh import: {import_error}"
    elif not _inside(Path(imported_from), REPO_SRC):
        row["failure"] = "post-refresh import no longer points into this repo's src"
    elif row["version"] != EXPECTED_VERSION:
        row["failure"] = f"doctor did not report version {EXPECTED_VERSION}"
    elif not row["enforcing"]:
        row["failure"] = "doctor did not report ENFORCING HERE: YES"
    elif doctor.returncode != 0:
        row["failure"] = f"doctor exited {doctor.returncode}"
    return row


def _other_user_home(path: Path, home: Path) -> bool:
    parts = path.resolve(strict=False).parts
    home_parts = home.resolve(strict=False).parts
    return len(parts) >= 3 and parts[1] == "Users" and parts[:3] != home_parts[:3]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-root",
        action="append",
        type=Path,
        default=None,
        help="root to scan (repeatable; replaces the default ~/Documents/claude scan root)",
    )
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--json", action="store_true", help="emit the complete machine-readable report")
    args = parser.parse_args()

    default_roots, exact_venvs, fleet_error = _venv_refresh_discovery_defaults()
    scan_roots = args.scan_root if args.scan_root is not None else default_roots
    candidates: list[Path] = list(exact_venvs)
    depth_pruned: list[Path] = []
    inaccessible_other_homes: list[str] = []
    home = Path.home()
    for root in scan_roots:
        root = root.expanduser()
        if _other_user_home(root, home) and not os.access(root, os.R_OK | os.X_OK):
            inaccessible_other_homes.append(str(root))
            continue
        candidates.extend(_discover_under(root, args.max_depth, depth_pruned))

    results: list[dict[str, object]] = []
    # Exact configured paths that do not exist are configuration skips, not discovered venvs.
    missing_exact = [str(path) for path in _dedupe(exact_venvs) if not _is_venv(path)]
    discovered = _dedupe(path for path in candidates if _is_venv(path))
    for venv in discovered:
        imported_from, error = _import_location(venv)
        if error:
            results.append({
                "venv": str(venv), "classification": "skipped", "refreshed": False, "reason": error
            })
        elif not _inside(Path(imported_from), REPO_SRC):
            results.append({
                "venv": str(venv),
                "classification": "flagged-separate-or-pinned",
                "refreshed": False,
                "imports_from": imported_from,
                "reason": "imports spendguard from a different path; left unchanged",
            })
        else:
            result = _repair_and_verify_explicit_venv(venv)
            result.setdefault("imports_from", imported_from)
            results.append(result)

    totals = {
        "discovered": len(discovered),
        "editable_refreshed_ok": sum(
            row["classification"] == "editable-shared-tree" and "failure" not in row for row in results
        ),
        "flagged": sum(row["classification"] == "flagged-separate-or-pinned" for row in results),
        "skipped": sum(row["classification"] == "skipped" for row in results),
        "failed": sum(row["classification"] == "editable-shared-tree" and "failure" in row for row in results),
    }
    report = {
        "repo_root": str(REPO_ROOT),
        "expected_version": EXPECTED_VERSION,
        "scan_roots": [str(path) for path in scan_roots],
        "fleet_registry_error": fleet_error,
        "missing_configured_venvs": missing_exact,
        "inaccessible_other_user_homes": inaccessible_other_homes,
        "depth_pruned": [str(path) for path in _dedupe(depth_pruned)],
        "results": results,
        "totals": totals,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for row in results:
            status = row.get("failure") or row.get("reason") or "ok"
            print(
                f"{row['venv']} | {row['classification']} | refreshed={row.get('refreshed', False)} "
                f"| version={row.get('version', '-')} | enforcing={row.get('enforcing', '-')} | {status}"
            )
        print("totals: " + " ".join(f"{key}={value}" for key, value in totals.items()))
        if fleet_error:
            print(f"fleet registry warning: {fleet_error}")
        for path in missing_exact:
            print(f"configured venv missing: {path}")
        for path in inaccessible_other_homes:
            print(f"other-user home inaccessible (not modified): {path}")
        for path in report["depth_pruned"]:
            print(f"depth limit pruned subtree (not scanned): {path}")
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
