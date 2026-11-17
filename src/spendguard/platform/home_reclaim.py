"""Fail-closed reclamation of stale, regenerable spendguard-home artifacts.

Name adjudication: ``spendguard reclaim`` is a distinct home-files command, not a
command group.  Ledger freelist compaction remains the deliberately separate
``scripts/ops/ledger_reclaim.py`` operation and is cross-referenced by CLI help.

Selection here is parsing, not judgement: configured directory layout, ages and
filename grammars fully determine membership.  Reasonable reviewers cannot
disagree once those inputs are fixed, so an LLM would add cost and uncertainty.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from spendguard.store.configuration import config


RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES = {
    "delegate_executions": {"dirname": "delegate-executions", "max_age_days": 7, "enabled": True},
    "delegate_worktrees": {"dirname": "delegate-worktrees", "max_age_days": 2, "enabled": True},
}
RECLAIM_DEFAULT_CACHE_DIRECTORIES = {
    "distribution_checkouts": {"dirname": "distribution-checkouts", "enabled": True},
    "distribution_wheels": {"dirname": "distribution-wheels", "enabled": True},
}
RECLAIM_DEFAULT_ROTATED_LOGS = {
    "enabled": True,
    "pattern": r"^lane_queue_archive\.jsonl\.[1-9][0-9]*$",
}
RECLAIM_DEFAULT_STATE_SNAPSHOTS = {
    "enabled": True,
    "pattern": r"^(?P<module>.+)_state\.(?P<stamp>[0-9]{8}T[0-9]{6}Z)\.(?:.+-state|save)\.json$",
    "timestamp_format": "%Y%m%dT%H%M%SZ",
    "keep_per_module": 1,
}
RECLAIM_DEFAULT_PROTECTION = {
    "exact_names": [
        "spend.db", "lane_queue.db", "delegate_jobs.db", "fleet.db",
        "capability_index.db", "gate_log.jsonl", "conv_index.json",
    ],
    "protected_directories": ["snapshots"],
    "live_state_pattern": r"^.+\.json~?$",
}
RECLAIM_LSOF_EXECUTABLE_DEFAULT = "lsof"
RECLAIM_SECONDS_PER_DAY = 24 * 60 * 60


class HomeReclaimRefusal(RuntimeError):
    """A candidate could not be proven safe; deletion is deliberately refused."""


def _reclaim_setting(key: str, default: Any) -> Any:
    return config._cfg_get("safety", f"reclaim.{key}", default)


def _resolved_reclaim_policy() -> dict[str, Any]:
    return {
        "ephemeral": _reclaim_setting("ephemeral", RECLAIM_DEFAULT_EPHEMERAL_DIRECTORIES),
        "caches": _reclaim_setting("caches", RECLAIM_DEFAULT_CACHE_DIRECTORIES),
        "rotated_logs": _reclaim_setting("rotated_logs", RECLAIM_DEFAULT_ROTATED_LOGS),
        "state_snapshots": _reclaim_setting("state_snapshots", RECLAIM_DEFAULT_STATE_SNAPSHOTS),
        "protection": _reclaim_setting("protection", RECLAIM_DEFAULT_PROTECTION),
        "lsof_executable": _reclaim_setting("lsof_executable", RECLAIM_LSOF_EXECUTABLE_DEFAULT),
    }


def _assert_reclaim_path_safe(
    path: Path,
    home: Path,
    protection: dict[str, Any],
    *,
    allowed_live_state_pattern: str | None = None,
) -> Path:
    home_real = home.resolve(strict=True)
    candidate_real = path.resolve(strict=True)
    try:
        relative = candidate_real.relative_to(home_real)
    except ValueError as error:
        raise HomeReclaimRefusal(f"outside configured home: {path}") from error
    if not relative.parts:
        raise HomeReclaimRefusal("configured home itself is protected")
    if relative.parts[0] in set(protection["protected_directories"]):
        raise HomeReclaimRefusal(f"protected directory: {path}")
    if path.name in set(protection["exact_names"]):
        raise HomeReclaimRefusal(f"protected live file: {path}")
    is_explicit_snapshot = (
        allowed_live_state_pattern is not None
        and re.fullmatch(allowed_live_state_pattern, path.name) is not None
    )
    if re.fullmatch(str(protection["live_state_pattern"]), path.name) and not is_explicit_snapshot:
        raise HomeReclaimRefusal(f"live JSON state is protected: {path}")
    return candidate_real


def _contained_tree_size(
    path: Path,
    home: Path,
    protection: dict[str, Any],
    *,
    allowed_live_state_pattern: str | None = None,
) -> int:
    _assert_reclaim_path_safe(
        path, home, protection, allowed_live_state_pattern=allowed_live_state_pattern,
    )
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = path.lstat().st_size
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *files):
            child = root_path / name
            _assert_reclaim_path_safe(child, home, protection)
            total += child.lstat().st_size
    return total


def _lsof_owner_output(path: Path, executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, str(path)], capture_output=True, text=True, check=False,
        )
    except OSError as error:
        raise HomeReclaimRefusal(f"owner check unavailable for {path}: {error}") from error
    # Deliberately decide from OUTPUT, never exit status: lsof commonly exits 1 for
    # "no owner", while a nonzero exit can still carry ownership/error evidence.
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if output:
        raise HomeReclaimRefusal(f"live owner or unclean owner check for {path}: {output}")
    return output


def _state_snapshot_candidates(home: Path, policy: dict[str, Any]) -> list[Path]:
    if not policy["enabled"]:
        return []
    grammar = re.compile(str(policy["pattern"]))
    by_module: dict[str, list[tuple[datetime, Path]]] = defaultdict(list)
    for path in home.iterdir():
        if not path.is_file():
            continue
        match = grammar.fullmatch(path.name)
        if match is None:
            continue
        stamp = datetime.strptime(match.group("stamp"), str(policy["timestamp_format"])).replace(
            tzinfo=timezone.utc
        )
        by_module[match.group("module")].append((stamp, path))
    keep = int(policy["keep_per_module"])
    if keep < 0:
        raise HomeReclaimRefusal("state snapshot keep count must be non-negative")
    return [
        path
        for snapshots in by_module.values()
        for _stamp, path in sorted(snapshots, key=lambda item: item[0], reverse=True)[keep:]
    ]


def _selected_reclaim_units(home: Path, policy: dict[str, Any], now: datetime) -> dict[str, list[Path]]:
    selected: dict[str, list[Path]] = defaultdict(list)
    now_timestamp = now.timestamp()
    for category, declaration in policy["ephemeral"].items():
        if not declaration["enabled"]:
            continue
        parent = home / str(declaration["dirname"])
        if not parent.exists():
            continue
        cutoff = now_timestamp - float(declaration["max_age_days"]) * RECLAIM_SECONDS_PER_DAY
        selected[category].extend(
            child for child in parent.iterdir() if child.is_dir() and child.stat().st_mtime < cutoff
        )
    for category, declaration in policy["caches"].items():
        if declaration["enabled"]:
            parent = home / str(declaration["dirname"])
            if parent.exists():
                selected[category].extend(parent.iterdir())
    rotated = policy["rotated_logs"]
    if rotated["enabled"]:
        grammar = re.compile(str(rotated["pattern"]))
        selected["rotated_logs"].extend(
            path for path in home.iterdir() if path.is_file() and grammar.fullmatch(path.name)
        )
    selected["state_snapshots"].extend(_state_snapshot_candidates(home, policy["state_snapshots"]))
    return dict(selected)


def reclaim_home_artifacts(
    *, apply: bool = False, home: str | None = None, now_utc: str | None = None,
) -> dict[str, Any]:
    """Select or delete configured stale home artifacts; dry-run is the default.

    ``now_utc`` is an ISO-8601 test/automation clock override. The report counts
    filesystem bytes selected and, on apply, bytes successfully removed.
    """
    reclaim_home = Path(home).expanduser() if home is not None else config.active_spendguard_home()
    reclaim_home = reclaim_home.resolve(strict=True)
    now = datetime.fromisoformat(now_utc) if now_utc else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise HomeReclaimRefusal("now_utc must include a timezone")
    policy = _resolved_reclaim_policy()
    selected = _selected_reclaim_units(reclaim_home, policy, now.astimezone(timezone.utc))
    report_categories: dict[str, dict[str, Any]] = {}
    accepted_by_category: dict[str, list[tuple[Path, int]]] = {}
    total_bytes = freed_bytes = 0
    refusals: list[dict[str, str]] = []
    ephemeral_categories = set(policy["ephemeral"])
    for category in (*policy["ephemeral"], *policy["caches"], "rotated_logs", "state_snapshots"):
        accepted: list[tuple[Path, int]] = []
        for path in selected.get(category, []):
            try:
                snapshot_pattern = (
                    str(policy["state_snapshots"]["pattern"])
                    if category == "state_snapshots"
                    else None
                )
                size = _contained_tree_size(
                    path,
                    reclaim_home,
                    policy["protection"],
                    allowed_live_state_pattern=snapshot_pattern,
                )
                if category in ephemeral_categories:
                    _lsof_owner_output(path, str(policy["lsof_executable"]))
                accepted.append((path, size))
            except (HomeReclaimRefusal, OSError) as error:
                refusals.append({"path": str(path), "reason": str(error)})
        category_bytes = sum(size for _path, size in accepted)
        total_bytes += category_bytes
        accepted_by_category[category] = accepted
        report_categories[category] = {
            "count": len(accepted), "bytes": category_bytes, "deleted": 0,
            "items": [str(path) for path, _size in accepted],
        }
    # A failed preflight closes the entire apply operation before any mutation.
    # Revalidation below additionally protects against races after preflight.
    if apply and not refusals:
        for category, accepted in accepted_by_category.items():
            for path, size in accepted:
                try:
                    snapshot_pattern = (
                        str(policy["state_snapshots"]["pattern"])
                        if category == "state_snapshots"
                        else None
                    )
                    _assert_reclaim_path_safe(
                        path,
                        reclaim_home,
                        policy["protection"],
                        allowed_live_state_pattern=snapshot_pattern,
                    )
                    if category in ephemeral_categories:
                        _lsof_owner_output(path, str(policy["lsof_executable"]))
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    freed_bytes += size
                    report_categories[category]["deleted"] += 1
                except (HomeReclaimRefusal, OSError) as error:
                    refusals.append({"path": str(path), "reason": str(error)})
                    break
            if refusals:
                break
    return {
        "home": str(reclaim_home), "apply": apply, "categories": report_categories,
        "total_count": sum(item["count"] for item in report_categories.values()),
        "total_bytes": total_bytes, "freed_bytes": freed_bytes, "refusals": refusals,
    }


def run_home_reclaim_cli(
    *, apply: bool = False, json_output: bool = False, home: str | None = None,
) -> int:
    """Render home reclamation; this does not run ledger VACUUM/recovery."""
    import json

    try:
        report = reclaim_home_artifacts(apply=apply, home=home)
    except (HomeReclaimRefusal, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}) if json_output else f"REFUSED: {error}")
        return 2
    if json_output:
        print(json.dumps(report, sort_keys=True))
    else:
        mode = "APPLY" if apply else "DRY-RUN"
        print(f"spendguard reclaim {mode} (home artifacts only; ledger VACUUM is scripts/ops/ledger_reclaim.py)")
        for category, result in report["categories"].items():
            print(f"{category}: {result['count']} item(s), {result['bytes']} bytes")
        print(f"total: {report['total_count']} item(s), {report['total_bytes']} bytes; freed {report['freed_bytes']} bytes")
        for refusal in report["refusals"]:
            print(f"REFUSED {refusal['path']}: {refusal['reason']}")
    return 2 if report["refusals"] else 0
