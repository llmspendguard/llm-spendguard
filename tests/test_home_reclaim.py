"""Home artifact reclamation selectors, fail-closed guards, mutation boundary, and surfaces."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard.platform import home_reclaim

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=8)


def _write(path: Path, payload: bytes = b"payload") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _age(path: Path, instant: datetime = OLD) -> None:
    timestamp = instant.timestamp()
    os.utime(path, (timestamp, timestamp))


def main() -> int:
    failures = []
    original_run = home_reclaim.subprocess.run
    original_setting = home_reclaim._reclaim_setting
    with tempfile.TemporaryDirectory(prefix="spendguard-reclaim-test-") as temporary:
        reclaim_home = Path(temporary)
        home_reclaim.subprocess.run = lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="", returncode=1)
        try:
            old_execution = _write(reclaim_home / "delegate-executions" / "old" / "journal")
            _age(old_execution.parent)
            recent_execution = _write(reclaim_home / "delegate-executions" / "recent" / "journal")
            old_worktree = _write(reclaim_home / "delegate-worktrees" / "old" / "tracked")
            _age(old_worktree.parent, NOW - timedelta(days=3))
            cache_item = _write(reclaim_home / "distribution-checkouts" / "checkout" / "data")
            wheel_item = _write(reclaim_home / "distribution-wheels" / "wheel.whl")
            rotated = _write(reclaim_home / "lane_queue_archive.jsonl.2")
            live_log = _write(reclaim_home / "lane_queue_archive.jsonl")
            newest = _write(reclaim_home / "router_state.20261117T000000Z.save.json")
            older = _write(reclaim_home / "router_state.20261116T000000Z.worker-state.json")
            live_state = _write(reclaim_home / "router_state.json")
            backup_state = _write(reclaim_home / "router_state.json~")

            dry = home_reclaim.reclaim_home_artifacts(home=str(reclaim_home), now_utc=NOW.isoformat())
            record_script_check(failures, "dry-run selects and counts bytes without deleting",
                                not dry["apply"] and dry["freed_bytes"] == 0 and dry["total_bytes"] > 0 and
                                old_execution.parent.exists() and cache_item.exists() and rotated.exists())
            expected_counts = {"delegate_executions": 1, "delegate_worktrees": 1,
                               "distribution_checkouts": 1, "distribution_wheels": 1,
                               "rotated_logs": 1, "state_snapshots": 1}
            record_script_check(failures, "positive and negative selectors are exact",
                                all(dry["categories"][key]["count"] == value
                                    for key, value in expected_counts.items()) and
                                dry["categories"]["state_snapshots"]["items"] == [str(older.resolve())] and
                                recent_execution.exists() and live_log.exists() and newest.exists() and
                                live_state.exists() and backup_state.exists())

            applied = home_reclaim.reclaim_home_artifacts(apply=True, home=str(reclaim_home), now_utc=NOW.isoformat())
            record_script_check(failures, "apply deletes only selected units and reports freed bytes",
                                applied["freed_bytes"] == applied["total_bytes"] > 0 and
                                not old_execution.parent.exists() and not old_worktree.parent.exists() and
                                not cache_item.exists() and not wheel_item.exists() and not rotated.exists() and
                                not older.exists() and recent_execution.exists() and live_log.exists() and
                                newest.exists() and live_state.exists() and backup_state.exists())

            protected = [_write(reclaim_home / "spend.db"),
                         _write(reclaim_home / "snapshots" / "recovery.db"),
                         _write(reclaim_home / "another_live.json")]
            outside = _write(reclaim_home.parent / f"{reclaim_home.name}-outside")
            refused = []
            for path in (*protected, outside):
                try:
                    home_reclaim._assert_reclaim_path_safe(path, reclaim_home,
                                                           home_reclaim.RECLAIM_DEFAULT_PROTECTION)
                except home_reclaim.HomeReclaimRefusal:
                    refused.append(path)
            outside.unlink()
            record_script_check(failures, "guard refuses ledger, snapshot, live JSON, and outside paths",
                                refused == [*protected, outside])

            custom_unit = _write(reclaim_home / "jobs-custom" / "old" / "data").parent
            _age(custom_unit, NOW - timedelta(days=2))
            custom = {"custom_jobs": {"dirname": "jobs-custom", "max_age_days": 1, "enabled": True}}
            home_reclaim._reclaim_setting = lambda key, default: custom if key == "ephemeral" else default
            overridden = home_reclaim.reclaim_home_artifacts(home=str(reclaim_home), now_utc=NOW.isoformat())
            record_script_check(failures, "config override changes selection without source edits",
                                overridden["categories"]["custom_jobs"]["items"] ==
                                [str(custom_unit.resolve())])
            home_reclaim._reclaim_setting = original_setting

            owned = _write(reclaim_home / "delegate-executions" / "owned" / "journal").parent
            _age(owned)
            home_reclaim.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
                stdout="COMMAND PID USER\npython 42 owner", stderr="", returncode=0)
            blocked = home_reclaim.reclaim_home_artifacts(apply=True, home=str(reclaim_home), now_utc=NOW.isoformat())
            record_script_check(failures, "lsof output blocks a live-owned ephemeral unit",
                                owned.exists() and blocked["categories"]["delegate_executions"]["count"] == 0 and
                                any("live owner" in item["reason"] for item in blocked["refusals"]))
        finally:
            home_reclaim.subprocess.run = original_run
            home_reclaim._reclaim_setting = original_setting

    import spendguard
    from spendguard.interface import mcp_server
    from spendguard.interface.capability_cli_callable_generation import registered_callable_cli_commands
    api_names = spendguard.CAPABILITY_API_COVERAGE["C283"]
    mcp_names = mcp_server.CAPABILITY_MCP_COVERAGE["C283"]
    record_script_check(failures, "capability SSOT generates CLI, API, and MCP parity",
                        "reclaim" in registered_callable_cli_commands() and len(api_names) == len(mcp_names) == 2 and
                        all(callable(getattr(spendguard, name)) for name in api_names) and
                        all(name in mcp_server._TOOLS for name in mcp_names))
    print(f"\n{'[FAIL]' if failures else 'OK'} test_home_reclaim: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
