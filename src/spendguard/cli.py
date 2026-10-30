"""spendguard CLI — one entry point for the whole toolkit.

  spendguard status | on | off          # kill-switch control
  spendguard report [--alert-threshold N]
  spendguard receipt [--json]             # running tally (today/7d/month); also auto-emitted per flow
  spendguard reconcile openai|anthropic [--since DATE] [--by-day]
  spendguard estimate --items N ...
  spendguard calibrate predict|show|pair|backtest   # LEARNED estimator: your history corrects the naive $
  spendguard audit [--ci]
  spendguard pricing                      # print the canonical table
"""

from .capability_cli_parsing import add_registered_cli_argument, registered_cli_option_token
import sys

_DEPLOY_HOME_ENVIRONMENT_KEY = "SPENDGUARD_HOME"
_DEPLOY_EDITABLE_INSTALL_ARGUMENTS = ("-m", "pip", "install", "-e", ".", "--no-cache-dir")
_DEPLOY_GATE_PATH_PARTS = ("scripts", "test", "chunked_suite.py")
_LANE_LEAK_TRIPWIRE_PATH_PARTS = ("scripts", "protected_lane_leak_tripwire.py")
_LANE_LEAK_TRIPWIRE_MODULE_NAME = "spendguard_protected_lane_leak_tripwire"


def _deploy_execute_subprocess(command, **kwargs):
    """Single subprocess seam for deploy's installer and gate; tests replace it, never the real host tools."""
    import subprocess
    return subprocess.run(command, **kwargs)


def _deploy_align_active_editable_install(repo_root):
    """Self-heal this interpreter's editable metadata before gating; fail closed unless alignment is restored."""
    from . import gate
    stale, code_version, metadata_version = gate._stale_editable_install()
    if not stale:
        return True, ""
    command = [sys.executable, *_DEPLOY_EDITABLE_INSTALL_ARGUMENTS]
    remediation = f"`{sys.executable} -m pip install -e . --no-cache-dir`"
    print("deploy: editable-install drift detected "
          f"(running code {code_version} != metadata {metadata_version}); self-healing with {remediation}…")
    try:
        completed = _deploy_execute_subprocess(command, cwd=str(repo_root), capture_output=True, text=True)
    except Exception as exc:
        return False, f"editable-install self-heal could not run; run {remediation}: {exc}"
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "pip install failed").strip().replace("\n", " ")
        return False, f"editable-install self-heal failed; run {remediation}: {detail}"
    still_stale, repaired_code_version, repaired_metadata_version = gate._stale_editable_install()
    if still_stale:
        return False, ("editable-install version drift remains after self-heal "
                       f"(running code {repaired_code_version} != metadata {repaired_metadata_version}); "
                       f"run {remediation}")
    print(f"deploy: editable-install metadata repaired ({repaired_code_version}).")
    return True, ""


def _deploy_run_hermetic_gate(suite_path):
    """Run the gate with an isolated child-only home; the parent retains its real promotion home."""
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix="spendguard-deploy-gate-") as isolated_home:
        gate_environment = os.environ.copy()
        gate_environment[_DEPLOY_HOME_ENVIRONMENT_KEY] = isolated_home
        return _deploy_execute_subprocess(
            [sys.executable, str(suite_path)], env=gate_environment).returncode


def _deploy_failed_verdict(reason, exit_code=1):
    """Render deploy's terminal fail-closed verdict."""
    print(f"🔴 DEPLOY FAILED — {reason} (pointer NOT advanced)", file=sys.stderr)
    return exit_code


def _deploy_succeeded_verdict(release_record):
    """Render deploy's terminal success verdict after propagation work completes."""
    print(f"🟢 DEPLOY SUCCEEDED — promoted {release_record['short']} ({release_record['describe']})")
    return 0

# Discovery and help share the registry's command topology and parameter schemas.


def _all_commands():
    from .capability_registry import CLI_COMMAND_GRAMMARS
    from .capability_cli_callable_generation import registered_callable_cli_commands

    return sorted({spelling for contract in CLI_COMMAND_GRAMMARS.values()
                   for spelling in (contract["command"], *contract["aliases"])}
                  | set(registered_callable_cli_commands()))


def help_text():
    from .capability_cli_help_generation import render_capability_cli_grouped_help

    return render_capability_cli_grouped_help().rstrip()


def ledger_read_cli(argv):
    """Parse the CLI skin of the same read operations exposed over MCP."""
    import argparse
    import json
    from .mcp_server import LEDGER_READ_OPERATIONS, _tool_ledger_read
    parser = argparse.ArgumentParser(prog="spendguard ledger-read")
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/operation', choices=LEDGER_READ_OPERATIONS)
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--table')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--event-id')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--since')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--until')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--path')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--group-by')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--conv')
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--deadline-s', type=float)
    add_registered_cli_argument(parser, 'ledger-read/cli.ledger_read_cli/parser/--json')
    args = vars(parser.parse_args(argv))
    if args["operation"] == "event" and not args["event_id"]:
        parser.error("event requires --event-id")
    from .ledger_read import LedgerReadError
    try:
        payload = _tool_ledger_read(args)
    except (LedgerReadError, ValueError) as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}",
                          "error_type": type(error).__name__}, default=str))
        return 1
    print(json.dumps(payload, default=str))
    return 0


REAP_UNREADABLE_SAMPLE_MAX = 20


def reap_orphaned_warm_servers_cli(argv):
    """Operator surface for the structural warm-server reaper; dry-run unless ``--execute`` is explicit.

    Exit contract: 0 means the scan completed cleanly (including dry runs that find orphans, or executes that reap
    every orphan); 1 means at least one process group remained alive after the reaper's SIGKILL grace. Unreadable
    processes are an explicit, visible classification gap but are not a failure: same-uid warm servers are readable,
    while other users' or root-owned processes may not be.
    """
    import argparse
    import json
    from .warm_stdio_daemon import reap_orphaned_warm_servers

    parser = argparse.ArgumentParser(prog="spendguard reap-orphans")
    add_registered_cli_argument(parser, 'reap-orphans/cli.reap_orphaned_warm_servers_cli/parser/--execute')
    add_registered_cli_argument(parser, 'reap-orphans/cli.reap_orphaned_warm_servers_cli/parser/--lane')
    add_registered_cli_argument(parser, 'reap-orphans/cli.reap_orphaned_warm_servers_cli/parser/--json')
    add_registered_cli_argument(parser, 'reap-orphans/cli.reap_orphaned_warm_servers_cli/parser/--bounded-log', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    report = reap_orphaned_warm_servers(lane=args.lane, execute=args.execute)
    marked = report.get("marked", [])
    lanes = {}
    for server in marked:
        lane_label = server.get("lane") or "(unlabelled)"
        lane_report = lanes.setdefault(lane_label, {"marked_servers": 0, "orphan_servers": 0, "owners": {}})
        lane_report["marked_servers"] += 1
        if server.get("orphan"):
            lane_report["orphan_servers"] += 1
        owner_key = str(server.get("owner_pid"))
        owner = lane_report["owners"].setdefault(
            owner_key, {"owner_pid": server.get("owner_pid"), "state": server.get("owner_state"), "servers": 0})
        owner["servers"] += 1
    for lane_report in lanes.values():
        lane_report["owners"] = list(lane_report["owners"].values())
    unreadable_count = len(report["unreadable"])
    unreadable_sample = report["unreadable"][:REAP_UNREADABLE_SAMPLE_MAX]
    payload = {"mode": "execute" if args.execute else "dry-run", "lane_filter": args.lane,
               "lanes": lanes, **{key: report[key] for key in
                                  ("scanned", "orphans", "reaped", "cross_uid_skipped", "errors")},
               "unreadable": unreadable_sample, "unreadable_count": unreadable_count,
               "unreadable_truncated": unreadable_count > len(unreadable_sample)}
    if args.json or args.bounded_log:
        rendered = json.dumps(payload, sort_keys=True)
        if args.bounded_log:
            from .fleet import append_bounded_fleet_log_record
            append_bounded_fleet_log_record(args.bounded_log, rendered)
        else:
            print(rendered)
    else:
        scope = args.lane or "all warm lanes"
        print(f"warm-server orphan sweep ({payload['mode']}; {scope})")
        if not lanes:
            print("  marked servers: 0")
        for lane_label, lane_report in sorted(lanes.items()):
            print(f"  {lane_label}: {lane_report['marked_servers']} marked server(s), "
                  f"{lane_report['orphan_servers']} provably-dead orphan(s)")
            for owner in lane_report["owners"]:
                print(f"    owner {owner['owner_pid']}: {owner['state']}; {owner['servers']} server(s)")
        action_label = "reaped" if args.execute else "would reap"
        print(f"  {action_label}: {len(report['reaped'] if args.execute else report['orphans'])}; "
              f"errors: {len(report['errors'])}; unreadable: {unreadable_count}")
        print(f"  {report['cross_uid_skipped']} cross-uid processes skipped (not spendguard-owned)")
        for gap in unreadable_sample:
            print(f"    unreadable: {gap}")
        if payload["unreadable_truncated"]:
            print(f"    unreadable: (+{unreadable_count - len(unreadable_sample)} more)")
        for error in report["errors"]:
            print(f"    error: {error}")
    return 1 if report["errors"] else 0


def manage_lane_queue_cli(argv):
    """Operator queue manager; exit 0 on a completed dry-run/execute and 1 only on a real manager failure."""
    import argparse
    import json
    from .lane_queue import manage_queue_tick

    parser = argparse.ArgumentParser(prog="spendguard queue-manage")
    add_registered_cli_argument(parser, 'queue-manage/cli.manage_lane_queue_cli/parser/--execute')
    add_registered_cli_argument(parser, 'queue-manage/cli.manage_lane_queue_cli/parser/--json')
    add_registered_cli_argument(parser, 'queue-manage/cli.manage_lane_queue_cli/parser/--no-process')
    add_registered_cli_argument(parser, 'queue-manage/cli.manage_lane_queue_cli/parser/--stale-days', type=float)
    add_registered_cli_argument(parser, 'queue-manage/cli.manage_lane_queue_cli/parser/--bounded-log', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    report = manage_queue_tick(execute=args.execute, process=not args.no_process, stale_days=args.stale_days)
    rendered = json.dumps(report, sort_keys=True)
    if args.bounded_log:
        from .fleet import append_bounded_fleet_log_record
        append_bounded_fleet_log_record(args.bounded_log, rendered)
    elif args.json:
        print(rendered)
    else:
        plan = report.get("plan", {})
        def _sum_queue_manager_groups(groups):
            return sum(group.get("count", 0) for group in groups or [])

        print(f"lane queue manager ({report['mode']})")
        print(f"  expired leases: {_sum_queue_manager_groups(plan.get('expired_leases'))}; "
              f"stale pending/expired-leased: {_sum_queue_manager_groups(plan.get('stale_live'))}; "
              f"terminal retention: {_sum_queue_manager_groups(plan.get('terminal_retention'))}")
        for group in plan.get("stale_live", []):
            print(f"    {group['state']} {group['intent']}: {group['count']} "
                  f"({group['oldest_ts']} .. {group['newest_ts']})")
        process_report = report.get("process", {})
        action = "invoked" if process_report.get("invoked") else "not invoked"
        if report["mode"] == "dry-run":
            action = "would invoke" if process_report.get("would_invoke") else "would not invoke"
        print(f"  bounded drain: {action}; ready pending: {plan.get('ready_pending', 0)}; "
              f"overloaded: {process_report.get('overloaded', False)}")
        if report.get("error"):
            print(f"  error: {report['error']}")
    return 1 if report.get("error") else 0


def load_protected_lane_tripwire_module():
    """Load the existing checkout-owned tripwire without duplicating its detection logic."""
    import importlib.util
    from pathlib import Path

    repository_root = Path(__file__).resolve().parents[2]
    tripwire_path = repository_root.joinpath(*_LANE_LEAK_TRIPWIRE_PATH_PARTS)
    module_spec = importlib.util.spec_from_file_location(_LANE_LEAK_TRIPWIRE_MODULE_NAME, tripwire_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"cannot load protected-lane tripwire from {tripwire_path}")
    tripwire_module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(tripwire_module)
    return tripwire_module


def lane_leak_check_cli(argv):
    """Run the existing protected-lane tripwire and preserve every terminal status."""
    import argparse

    parser = argparse.ArgumentParser(prog="spendguard lane-leak-check")
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--protect-executor')
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--protect-provider')
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--hours', type=float)
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--min-other-calls', type=int)
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--ledger-path')
    add_registered_cli_argument(parser, 'lane-leak-check/cli.lane_leak_check_cli/parser/--bounded-log', help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if bool(arguments.protect_executor) != bool(arguments.protect_provider):
        parser.error("--protect-executor and --protect-provider must be supplied together")

    return check_protected_lane_leaks(**vars(arguments))


def check_protected_lane_leaks(*, protect_executor: str | None = None,
                               protect_provider: str | None = None, hours: float | None = None,
                               min_other_calls: int | None = None, ledger_path: str | None = None,
                               bounded_log: str | None = None) -> int:
    """Run the existing protected-lane tripwire with its complete terminal precedence."""
    import contextlib
    import io
    from types import SimpleNamespace
    from . import config, lane_catalog
    arguments = SimpleNamespace(protect_executor=protect_executor, protect_provider=protect_provider,
                                hours=hours, min_other_calls=min_other_calls, ledger_path=ledger_path,
                                bounded_log=bounded_log)
    tripwire_module = load_protected_lane_tripwire_module()
    if arguments.protect_executor:
        protected_targets = ((arguments.protect_executor, arguments.protect_provider),)
    else:
        protected_targets = tuple(
            (lane, lane_catalog.lane_provider(lane)) for lane in sorted(config.shed_lanes()))

    rendered_stream = io.StringIO()
    exit_codes = []
    with contextlib.redirect_stdout(rendered_stream), contextlib.redirect_stderr(rendered_stream):
        if not protected_targets:
            print("INCONCLUSIVE: no protected lanes are configured in advisor.shed_lanes")
            exit_codes.append(tripwire_module.INCONCLUSIVE_EXIT)
        for protected_executor, protected_provider in protected_targets:
            if not protected_provider:
                print(f"PROBE FAILED: no provider is registered for protected lane {protected_executor!r}")
                exit_codes.append(tripwire_module.PROBE_FAILED_EXIT)
                continue
            tripwire_arguments = ["--protect-executor", protected_executor,
                                  "--protect-provider", protected_provider]
            if arguments.hours is not None:
                tripwire_arguments.extend(("--hours", str(arguments.hours)))
            if arguments.min_other_calls is not None:
                tripwire_arguments.extend(("--min-other-calls", str(arguments.min_other_calls)))
            if arguments.ledger_path is not None:
                tripwire_arguments.extend(("--ledger-path", arguments.ledger_path))
            exit_codes.append(tripwire_module.main(tripwire_arguments))
    rendered = rendered_stream.getvalue()
    if arguments.bounded_log:
        from .fleet import append_bounded_fleet_log_record
        append_bounded_fleet_log_record(arguments.bounded_log, rendered)
    else:
        print(rendered, end="")
    for terminal_code in (tripwire_module.LEAK_EXIT, tripwire_module.PROBE_FAILED_EXIT,
                          tripwire_module.INCONCLUSIVE_EXIT, tripwire_module.CLEAN_EXIT):
        if terminal_code in exit_codes:
            return terminal_code
    return tripwire_module.PROBE_FAILED_EXIT


def cli_health_command(argv):
    """Expose the MCP health callable without duplicating probes or health decisions."""
    import argparse
    import json
    from . import mcp_server

    parser = argparse.ArgumentParser(prog="spendguard health")
    add_registered_cli_argument(parser, 'health/cli.cli_health_command/parser/--run', action=argparse.BooleanOptionalAction, default=mcp_server.HEALTH_DEFAULT_RUN)
    add_registered_cli_argument(parser, 'health/cli.cli_health_command/parser/--timeout_s', type=int, default=mcp_server.HEALTH_DEFAULT_TIMEOUT_S)
    add_registered_cli_argument(parser, 'health/cli.cli_health_command/parser/--json')
    arguments = parser.parse_args(argv)
    try:
        result = mcp_server._tool_health({"run": arguments.run, "timeout_s": arguments.timeout_s})
    except Exception as exc:
        print(f"spendguard health: health call could not run: {exc!r}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(result))
    else:
        print(result.get("note", ""))
        for resource_kind in ("lanes", "metered"):
            for resource_name, resource_status in result[resource_kind].items():
                state = "ready" if resource_status.get("reachable") else "unreachable"
                latency = resource_status.get("latency")
                elapsed = f"{latency}s" if latency is not None else "unavailable"
                reason = resource_status.get("reason")
                detail = f" — {reason}" if reason else ""
                print(f"  {resource_kind} {resource_name}: {state}  latency={elapsed}{detail}")
        ledger_status = result.get("ledger", {})
        ledger_age = ledger_status.get("age_s")
        ledger_age_detail = f" (as of {ledger_age}s ago)" if ledger_age is not None else ""
        print(f"ledger: {ledger_status.get('line', 'UNKNOWN — health call did not report ledger status')}{ledger_age_detail}")
        if ledger_status.get("error"):
            print(f"  ledger diagnostic: {ledger_status['error']}")
        for action in result.get("actions", []):
            print(f"  {action['resource']}: {action.get('issue')} — {action.get('fix')}")
            if action.get("command"):
                print(f"    {action['command']}")
        if result.get("diagnostic"):
            print(f"diagnostic: {json.dumps(result['diagnostic'])}")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return 1 if result.get("error") else 0


def main(argv=None):
    """CLI entry. Wraps the dispatch so a MISSING PREREQUISITE (no provider key, no config) exits with one clean
    line instead of a raw traceback: `spendguard report` on a fresh install used to dump 14 lines ending in
    `KeyMissing`, while the reconcile branch caught the identical condition properly. A first run must never look
    like a crash — that is the whole first impression."""
    try:
        return _dispatch(argv)
    except RuntimeError as e:                             # KeyMissing subclasses RuntimeError
        print(f"spendguard: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:                               # `spendguard report | head` — not an error
        return 0


def _dispatch(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    # Package import normally auto-installs the gate.  CLI bootstrap alone defers that expensive public surface so
    # help can remain a pure parser/introspection path; every real command hydrates the same API and installs the
    # same gate immediately below.  This mirrors the command handlers' established local-import pattern.
    if cmd in ("--help", "-h", "help", "--commands"):
        print(help_text())
        return 0
    if any(arg in ("--help", "-h") for arg in rest):
        from .capability_cli_help_generation import render_capability_cli_help

        print(render_capability_cli_help(cmd), end="")
        return 0
    # Verify-only distribution must reach attestation before gate hydration: even
    # gate installation registers this process in the persistent fleet database.
    if cmd == "distribute":
        from .distribution import distribute_command, parse_distribution_cli_arguments
        if parse_distribution_cli_arguments(rest).check:
            return distribute_command(rest)
    from . import _load_public_api_and_install_gate
    _load_public_api_and_install_gate()
    # ensure the gate is installed for this process so the advisor's OWN LLM calls (optimize/experiment/
    # reconstruct/mine/review/promote/cascade/brief --llm) are caged by caps.meta even when run via the CLI
    # outside a gated venv. Installation failures must propagate before dispatch.
    from . import gate
    gate.install()
    if cmd in ("status", "doctor"):                   # first-run nudge when nothing's configured yet
        try:
            from . import config
            if not config.CONFIG_JSON.exists() and not config.saas_path().exists():
                print("ℹ not configured yet — run `spendguard init --all` for one-command setup (caps + $0 lanes + "
                      "gate/MCP/receipt/rule/skills); plain `spendguard init` is config-only. Works standalone.\n")
        except Exception:
            pass
    from .capability_cli_dispatch_generation import invoke_registered_cli_command
    matched, result = invoke_registered_cli_command(cmd, rest, argv)
    if matched:
        return result
    import difflib
    near = difflib.get_close_matches(cmd, _all_commands(), n=3, cutoff=0.55)
    print(f"unknown command {cmd!r}" + (f" — did you mean: {', '.join(near)}?" if near else ""), file=sys.stderr)
    print("`spendguard --help` lists every command.", file=sys.stderr)
    return 2


def cli_core_status(rest, cmd, argv):
        from . import gate
        return gate._cli(cmd, live=registered_cli_option_token('doctor+status/--live') in rest)   # doctor --live forces the full provider pull


def cli_core_reconcile(rest, cmd, argv):
        _PROV = {"openai", "anthropic", "all"}
        # A bare `spendguard reconcile` defaults to openai. But a FLAG in the first slot (e.g. `--since 2026-08-01`)
        # is NOT a provider — consuming it as `sub` silently ate the flag AND still ran openai; treat it as an
        # argument and keep the default. A non-flag word that isn't a known provider is a typo — error out rather
        # than silently run openai and print the wrong provider's numbers under the name the user typed.
        if rest and not rest[0].startswith("-"):
            sub, prov_args = rest[0], rest[1:]
            if sub not in _PROV:
                print(f"reconcile: unknown provider {sub!r} — choose one of {', '.join(sorted(_PROV))} "
                      f"(or omit it for openai)", file=sys.stderr)
                return 2
        else:
            sub, prov_args = "openai", rest
        if sub == "all":                                  # unified view: every spend source through the one loop
            from . import reconcile
            _since = None                                 # honor --since here too: it was parsed into prov_args but
            if registered_cli_option_token('reconcile/--since') in prov_args:                    # never passed on, so `reconcile all --since DATE` silently
                _i = prov_args.index(registered_cli_option_token('reconcile/--since'))           # reported the DEFAULT window instead of the requested one
                _since = prov_args[_i + 1] if _i + 1 < len(prov_args) else None
            reconcile.report(since=_since)
            return 0
        if sub == "anthropic":
            from . import reconcile_anthropic as r
        else:
            from . import reconcile_openai as r
        sys.argv = ["reconcile"] + prov_args
        try:
            return r.main()
        except RuntimeError as e:                          # e.g. a missing provider key — clean one-line exit, no traceback
            print(e)
            return 1


def cli_core_deploy(rest, cmd, argv):
        import pathlib as _pl
        from . import config as _config
        from . import release as _rel
        r = list(rest)
        if "--help" in r or "-h" in r:
            # Help must NEVER acquire the deploy lock or run the gate. Before this, `deploy --help` fell through the
            # manual arg handling, took the single-instance lock, and RAN the full gate/promote — and a hung --help
            # then held the lock, blocking real deploys with "another deploy already running" (hit live 2026-10-10).
            print("usage: spendguard deploy [--no-gate] [--allow-dirty] [--no-restart] [--no-refresh]\n"
                  "                         [--skip-consumer-verify] [--no-auto-arm] [--dry-run]\n"
                  "  Promote the current committed HEAD to the green pointer so running MCP servers + gated consumers\n"
                  "  roll onto it. Runs the full gate (chunked_suite) first; the pointer only advances if it passes.\n"
                  "    --no-gate      trust a just-run gate; skip re-running the suite\n"
                  "    --allow-dirty  promote an uncommitted tree (discouraged — the pointer then names no commit)\n"
                  "    --no-restart   do not restart stale launchd-managed fleet consumers after promoting\n"
                  "    --no-refresh   do not refresh stale editable-install dependency metadata after promoting\n"
                  "    --skip-consumer-verify  skip deterministic workspace consumer verification\n"
                  "    --no-auto-arm  audit consumers without arming safely-fixable venvs\n"
                  "    --dry-run      show which safely-fixable venvs would be armed; write no hooks")
            return 0
        _no_gate, _allow_dirty = (registered_cli_option_token('deploy/--no-gate') in r), (registered_cli_option_token('deploy/--allow-dirty') in r)
        if _rel._tracked_dirty() and not _allow_dirty:
            print("deploy: the working tree has uncommitted changes to tracked files — commit first (the green "
                  "pointer must name committed code), or pass --allow-dirty deliberately.", file=sys.stderr)
            return _deploy_failed_verdict("working tree has uncommitted tracked changes", exit_code=2)
        # SINGLE-INSTANCE: only ONE deploy gate may run on this machine at a time. Two concurrent gates each spawn a
        # full test suite; they starve each other's CPU and the receipt/timeout-sensitive tests flake (measured
        # 2026-10-09 — two accidental `deploy` runs raced and both would have flaked). A count-1 semaphore (flock) held
        # for the whole gate+promote body makes a second invocation fail FAST instead of piling on.
        with _config.single_instance_lock("deploy") as _got_lock:
            if not _got_lock:
                print("deploy: another `spendguard deploy` is already running on this machine — only one gate may run "
                      "at a time (concurrent suites starve each other's CPU and flake). Wait for it to finish, then "
                      "re-run `spendguard deploy`.", file=sys.stderr)
                return _deploy_failed_verdict("another deploy already holds the deploy lock", exit_code=2)
            repo_root = _pl.Path(__file__).resolve().parents[2]
            install_aligned, install_failure = _deploy_align_active_editable_install(repo_root)
            if not install_aligned:
                return _deploy_failed_verdict(install_failure)
            gate_desc = "skipped (--no-gate)"
            if not _no_gate:
                suite = repo_root.joinpath(*_DEPLOY_GATE_PATH_PARTS)
                print(f"deploy: running the full gate ({suite.name}) — a few minutes; the pointer only advances if it passes…")
                if _deploy_run_hermetic_gate(suite) != 0:
                    print("deploy: 🔴 the gate FAILED — NOT promoting. Fix the suite, then re-run `spendguard deploy`.",
                          file=sys.stderr)
                    return _deploy_failed_verdict("chunked_suite gate failed")
                gate_desc = "chunked_suite green"
            try:
                rec = _rel.promote_release(gate=gate_desc, allow_dirty=_allow_dirty)
            except Exception as exc:
                return _deploy_failed_verdict(f"green-pointer promotion failed: {exc}")
        print(f"deploy: 🟢 promoted {rec['short']} ({rec['describe']}) → green pointer  [{rec['gate']}]")
        print("        already-running MCP servers serve it on their next request (they hand off between requests).")
        if registered_cli_option_token('deploy/--skip-consumer-verify') not in r:
            from .consumer_audit import audit_consumer_workspaces, render_consumer_audit_table
            _consumer_result = audit_consumer_workspaces(
                rec["sha"], auto_arm=registered_cli_option_token('deploy/--no-auto-arm') not in r, dry_run=registered_cli_option_token('deploy/--dry-run') in r,
            )
            print(render_consumer_audit_table(_consumer_result))
            if _consumer_result["remaining_gaps"]:
                print(f"deploy: promoted; armed {_consumer_result['armed']}; "
                      f"{_consumer_result['remaining_gaps']} consumers STILL not on latest/gated", file=sys.stderr)
                return 1
        from . import fleet as _fleet
        try:
            for _job in _fleet.install_periodic_launchd_fleet_jobs():
                _state = "installed" if _job["kickstart_rc"] == 0 else "install/kickstart warning"
                print(f"deploy: fleet job — {_state} {_job['label']} ({_job['path']})")
        except Exception as _job_error:
            print(f"deploy: ⚠ periodic fleet-job install failed: {_job_error}", file=sys.stderr)
        # UPDATE PROPAGATION: the MCP fleet self-respawns onto the green commit, but OTHER long-lived consumers
        # (7thsense/honestreview daemons, the drain) load spendguard ONCE at startup and would keep running stale code
        # (measured 2026-10-10: pre-fix daemons false-fired the codex toast for days). Restart the launchd-MANAGED
        # stale consumers now (they respawn onto latest); WARN loudly about any that are NOT supervised (never kill a
        # process we cannot bring back). `--no-restart` skips this.
        if registered_cli_option_token('deploy/--no-restart') not in r:
            _fr = _fleet.restart_stale(apply=True)
            for _x in _fr.get("restarted", []):
                print(f"deploy: fleet — {_x.get('action')} {_x.get('launchd_label')} (pid {_x.get('pid')})")
            if _fr.get(_fleet.FLEET_RESTART_REQUESTED_KEY):
                print("deploy: fleet — restarts requested; run `spendguard distribute --check` to confirm convergence.")
            for _f in _fr.get(_fleet.FLEET_RESTART_FAILURE_KEY, []):
                print(f"deploy: 🔴 fleet restart FAILED {_f.get('launchd_label')} (pid {_f.get('pid')}) — "
                      f"{_f.get('why')}", file=sys.stderr)
            for _w in _fr.get("warn", []):
                print(f"deploy: ⚠ STALE consumer pid {_w.get('pid')} — {_w.get('why')}: {str(_w.get('cmdline'))[:60]}",
                      file=sys.stderr)
            for _t in _fr.get(_fleet.FLEET_RESTART_TRANSIENT_KEY, []):
                print(f"deploy: fleet — transient one-shot, self-exiting: pid {_t.get('pid')} "
                      f"{str(_t.get('cmdline'))[:60]}")
            if (not _fr.get("restarted") and not _fr.get("warn") and not _fr.get(_fleet.FLEET_RESTART_FAILURE_KEY)
                    and not _fr.get(_fleet.FLEET_RESTART_TRANSIENT_KEY)):
                print("deploy: fleet — all tracked consumers already on the green commit.")
            if _fr.get(_fleet.FLEET_RESTART_FAILURE_KEY):
                return _deploy_failed_verdict("one or more supervised consumer restarts could not be requested")
        _refresh_deployments = registered_cli_option_token('deploy/--no-refresh') not in r
        _host_install_audit = _fleet.reconcile_registered_host_installations()
        _host_escape_count = len(_host_install_audit["escapes"])
        _host_install_failures = []
        if _refresh_deployments:
            from .setup import install_hook as _install_host_gate
            for _escape in _host_install_audit["escapes"]:
                _interpreter = _escape["python_executable"]
                try:
                    if _install_host_gate(user=True, python=_interpreter) != 0:
                        _host_install_failures.append({"python_executable": _interpreter,
                                                       "error": "install-hook did not establish enforcement"})
                except Exception as _escape_error:
                    _host_install_failures.append({"python_executable": _interpreter,
                                                   "error": str(_escape_error)})
            _host_install_audit = _fleet.reconcile_registered_host_installations()
        print("deploy: registry — "
              f"discovered={len(_host_install_audit['discovered'])} "
              f"reconciled={_host_escape_count - len(_host_install_audit['escapes'])} "
              f"remaining={len(_host_install_audit['escapes'])}")
        for _escape in _host_install_audit["escapes"]:
            print(f"deploy: 🔴 registry escape {_escape['python_executable']}: {_escape['remediation']}",
                  file=sys.stderr)
        for _failure in _host_install_failures:
            print(f"deploy: 🔴 registry escape FAILED {_failure['python_executable']}: {_failure['error']}",
                  file=sys.stderr)
        _deployment_verdict = _fleet.verify_and_refresh_registered_interpreters(apply=_refresh_deployments)
        print("deploy: completeness — "
              f"N_total={_deployment_verdict['N_total']} N_updated={_deployment_verdict['N_updated']} "
              f"N_verified_fresh={_deployment_verdict['N_verified_fresh']}")
        for _failure in _deployment_verdict["failed"]:
            print(f"deploy: 🔴 registered deployment FAILED {_failure.get('python_executable')}: "
                  f"{_failure.get('error') or _failure.get('state')}", file=sys.stderr)
        if _host_install_failures or not _host_install_audit["complete"] or not _deployment_verdict["complete"]:
            return _deploy_failed_verdict(
                "one or more host installations remain escaped or registered deployments remain stale or unreachable"
            )
        from .distribution import render_distribution_table
        _distribution = _rel.converge_deployed_release(dry_run=registered_cli_option_token('deploy/--dry-run') in r)
        print(render_distribution_table(_distribution))
        if _distribution["exit_code"]:
            print("🔴 DEPLOY INCOMPLETE — pointer promoted; distribution has named stragglers", file=sys.stderr)
            return _distribution["exit_code"]
        return _deploy_succeeded_verdict(rec)


def cli_core_fleet(rest, cmd, argv):
        import json as _json
        from . import fleet as _fleet
        if rest and rest[0] == "deregister":
            if len(rest) != 2:
                print("usage: spendguard fleet deregister <python_executable>", file=sys.stderr)
                return 2
            print(_json.dumps(_fleet.deregister_interpreter(rest[1]), indent=2))
            return 0
        if rest and rest[0] == "consumers":
            from . import release as _release

            consumers = _fleet.running_consumers(prune=False)
            green_sha = (_release.green_pointer() or {}).get("sha") or "—"
            if registered_cli_option_token('fleet/--json') in rest:
                print(_json.dumps(consumers, indent=2, default=str))
                return 0
            for consumer in consumers:
                print(f"{consumer['pid']} identity={consumer.get('pid_create_time')} role={consumer.get('role')} "
                      f"supervision={consumer.get('supervision_mode') or 'none'} "
                      f"ref={consumer.get('supervision_ref') or '—'} served={consumer.get('served_sha') or '—'} "
                      f"green={green_sha} "
                      f"liveness={consumer.get('liveness')} classification={consumer.get('classification')}")
            return 0
        if rest and rest[0] == "restart":
            selections = sum(flag in rest for flag in (registered_cli_option_token('fleet/--consumer'), registered_cli_option_token('fleet/--stale'), registered_cli_option_token('fleet/--all-supervised')))
            if selections != 1:
                print("usage: spendguard fleet restart [--consumer <id> | --stale | --all-supervised] [--apply]",
                      file=sys.stderr)
                return 2
            selected_consumer = None
            if registered_cli_option_token('fleet/--consumer') in rest:
                selected_index = rest.index(registered_cli_option_token('fleet/--consumer'))
                if selected_index + 1 >= len(rest):
                    print("fleet restart: --consumer requires an id", file=sys.stderr)
                    return 2
                selected_consumer = rest[selected_index + 1]
            result = _fleet.restart_stale(apply=registered_cli_option_token('fleet/--apply') in rest, consumer_id=selected_consumer,
                                          all_supervised=registered_cli_option_token('fleet/--all-supervised') in rest)
            print(_json.dumps(result, indent=2))
            return 1 if result.get(_fleet.FLEET_RESTART_FAILURE_KEY) else 0
        if rest and rest[0] == "stop":
            if registered_cli_option_token('fleet/--consumer') not in rest or rest.index(registered_cli_option_token('fleet/--consumer')) + 1 >= len(rest):
                print("usage: spendguard fleet stop --consumer <id>", file=sys.stderr)
                return 2
            selected_consumer = rest[rest.index(registered_cli_option_token('fleet/--consumer')) + 1]
            result = _fleet.stop_supervised_consumer(selected_consumer, apply=True)
            print(_json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        if registered_cli_option_token('fleet/--restart') in rest:
            res = _fleet.restart_stale(apply=registered_cli_option_token('fleet/--dry-run') not in rest)
            print(_json.dumps(res, indent=2))
            return 0
        summ = _fleet.fleet_summary()
        if registered_cli_option_token('fleet/--json') in rest:
            print(_json.dumps(summ, indent=2, default=str))
            return 0
        print(f"spendguard fleet — green pointer {summ['green']}; {summ['n']} live consumer(s), {summ['stale']} STALE")
        for c in summ["consumers"]:
            tag = "STALE" if c.get("stale") else ("?" if c.get("stale") is None else "ok")
            print(f"  [{tag:<5}] pid {c['pid']:<7} v{c.get('version')} @{c.get('served_short') or '—'}"
                  f"{' (dirty)' if c.get('dirty') else ''}  {str(c.get('cmdline'))[:60]}")
        if summ["stale"]:
            print("  → `spendguard fleet --restart` restarts the launchd-managed stale ones; others need a manual restart.")
        return 0


def cli_core_release(rest, cmd, argv):
        import json as _json
        from . import release as _rel
        st = _rel.release_status()
        attestation = _rel.converge_deployed_release(check=registered_cli_option_token('release/--check') in rest, dry_run=registered_cli_option_token('release/--dry-run') in rest)
        if registered_cli_option_token('release/--json') in rest:
            print(_json.dumps({**st, "distribution": attestation}, indent=2, default=str))
            return attestation["exit_code"]
        s, g = st.get("served") or {}, st.get("green") or {}
        print(f"served : {s.get('describe') or s.get('short') or '(unknown — not a git checkout)'}"
              + ("  [dirty: serving uncommitted edits]" if st.get("served_dirty") else ""))
        print(f"green  : {(g.get('describe') or g.get('short')) if g else '(none promoted — run `spendguard deploy`)'}")
        print(f"status : {'🔴 STALE' if st.get('stale') else ('🟢 up to date' if g else '⚪ no pointer yet')}")
        print(f"         {st.get('note')}")
        print(f"pointer: {st.get('pointer_path')}")
        from .distribution import render_distribution_table
        print(render_distribution_table(attestation))
        return attestation["exit_code"]


def cli_core_migrate(rest, cmd, argv):
        from . import migrate_charges
        import json as _json
        stats = migrate_charges.run_cutover()
        if stats.get("already_cutover"):
            print("✓ already cut over — `charges` is retired and spend_events is the sole money-of-record. "
                  "Nothing to migrate; nothing was touched.")
            return 0
        keys = ("charges_rows", "migrated", "skipped_zero", "db_snapshot", "backup_table",
                "src_exact", "dst_exact", "residual", "reconciles")
        print(_json.dumps({k: stats.get(k) for k in keys}, indent=2))
        if not stats.get("reconciles"):
            print("REFUSED to trust: Σ charges != Σ spend_events. Investigate before removing charges.")
            return 1
        return 0


def cli_core_providers(rest, cmd, argv):
        from . import pricing as p
        for prov, models in sorted(p.providers().items()):
            print(f"{prov} ({len(models)}): {', '.join(sorted(models))}")
        return 0


def cli_core_maxtokens(rest, cmd, argv):
        from . import bulkgate
        if not rest:
            print("usage: spendguard maxtokens <sig> [current_max] [provider:model]   (sig from a TRUNCATED warning, "
                  "or bulkgate.sig(...); pass the model to get a reasoning-inclusive seed when the class is unmeasured)")
            return 2
        cur = next((int(a) for a in rest[1:] if str(a).isdigit()), None)
        _model = next((a for a in rest[1:] if ":" in str(a)), None)   # provider:model → reasoning-aware seed (#1)
        mt = bulkgate.maxtokens(rest[0], current_max=cur, model=_model)
        if not mt.get("n"):
            if mt.get("recommend"):        # unmeasured but a reasoning model → a reasoning-INCLUSIVE seed, not a guess
                print(f"no measured outputs for sig {rest[0]} yet — SEEDED (reasoning-inclusive): recommend "
                      f"max_tokens = {mt['recommend']}")
            else:
                print(f"no observed outputs for sig {rest[0]} yet (run a few calls first; truncations seen: "
                      f"{mt.get('truncations',0)}). Pass the provider:model to seed a reasoning-aware estimate.")
            if mt.get("warn"):
                print(f"  ⚠ {mt['warn']}")
            return 0
        print(f"sig {mt['sig']}  n={mt['n']}  truncations={mt['truncations']}")
        print(f"  output tokens: p50={mt['p50']}  p95={mt['p95']}  p99={mt['p99']}  max={mt['max']}")
        print(f"  → recommend max_tokens = {mt['recommend']}  (p99 × 1.5 — measured, not guessed)")
        if mt.get("warn"):
            print(f"  ⚠ {mt['warn']}")
        return 0


def cli_core_plan_queue(rest, cmd, argv):
        from . import queue_planner                      # $0, NO execution — decides; the executor (drain) acts on it.
        if registered_cli_option_token('plan-queue/--watch') in rest:                            # CONTINUOUS ~1s look-ahead (Ash: "periodic say every second")
            import time as _tw
            _iv = next((rest[i + 1] for i, a in enumerate(rest) if a == registered_cli_option_token('plan-queue/--interval') and i + 1 < len(rest)), None)
            _tk = next((rest[i + 1] for i, a in enumerate(rest) if a == registered_cli_option_token('plan-queue/--ticks') and i + 1 < len(rest)), None)

            def _observe(tk):
                fc = tk.get("forecast") or {}
                at_risk = fc.get("at_risk") or []
                off, pace = tk.get("offload") or [], tk.get("pace") or []
                ts = _tw.strftime("%H:%M:%S")
                if tk.get("error"):
                    print("[%s] planner tick error: %s" % (ts, tk["error"])); return
                print("[%s] 429-risk=%s  offload=%d  pace=%d" % (
                    ts, (", ".join(at_risk) if at_risk else "none"), len(off), len(pace)))
                if at_risk:
                    print("  ⚠ PREDICTED 429: %s — %s" % (", ".join(at_risk),
                          ("offload %s to Batch API" % ", ".join(o["intent"] for o in off)) if off
                          else "PACE (governor spreading; no batch-eligible backlog)"))
            iv = float(_iv) if _iv else None
            print("planner watch: tick every %.1fs ($0, NO execution — the drain acts on it; Ctrl-C to stop)"
                  % (iv or queue_planner.PLANNER_INTERVAL_S_DEFAULT))
            res = queue_planner.plan_loop(interval_s=iv, iterations=(int(_tk) if _tk else None), on_tick=_observe)
            print("planner watch stopped (%s) after %d tick(s)." % (res["stopped_by"], res["ticks"]))
            return 0
        t = queue_planner.tick()
        try:
            from . import batch_tracker                   # attach the async BATCH-JOB tracker state to the plan (C3b)
            t["batch_jobs"] = batch_tracker.status()
        except Exception:
            t["batch_jobs"] = {}
        if registered_cli_option_token('plan-queue/--json') in rest:
            import json as _json
            print(_json.dumps(t, indent=2, default=str))
            return 0
        print(queue_planner.forecast_summary(t["forecast"]))
        off = t.get("offload") or []
        print("\noffload plan (%s):" % ("batch-eligible" if t.get("batch_eligible")
                                        else "DISABLED — no advisor.batch_model set"))
        if not off:
            print("  (nothing to offload — no saturated or cheaper-as-batch intent in the realtime backlog)")
        for o in off:
            print("  %-22s n=%-5d vendor=%-10s → %d chunk(s) ≤%s on %s  [%s]"
                  % (o["intent"], o["n"], o.get("vendor"), len(o["chunks"]),
                     (max(o["chunks"]) if o["chunks"] else 0), o.get("batch_model"),
                     "; ".join(o.get("reasons") or [])))
        if t.get("pace"):
            print("\npace (approaching TPM — the governor is already spreading these): %s" % ", ".join(t["pace"]))
        if t.get("skipped"):
            print("\nskipped (unpriceable this tick — left realtime, NOT dropped): %s"
                  % ", ".join("%s (%s)" % (s["intent"], s["reason"]) for s in t["skipped"]))
        bs = t.get("batch_jobs") or {}                   # BATCH-JOB tracker state — the async side of the plan (C3b)
        if bs.get("total"):
            print("\nbatch jobs (async, tracked): open=%d failing=%d settled=%d failed=%d  (total %d) — the drain "
                  "polls these; expired/failed fall over to realtime" % (bs.get("open", 0), bs.get("failing", 0),
                  bs.get("settled", 0), bs.get("failed", 0), bs.get("total", 0)))
        return 0


def cli_core_dispatch(rest, cmd, argv):
        from . import dispatch, lane_queue               # (same dispatch.admission_state() the MCP tool returns)
        if registered_cli_option_token('dispatch/--id') in rest:
            import argparse as _argparse
            import json as _json
            parser = _argparse.ArgumentParser(prog="spendguard dispatch")
            add_registered_cli_argument(parser, 'dispatch/cli._dispatch/parser/--id', type=int)
            add_registered_cli_argument(parser, 'dispatch/cli._dispatch/parser/--json')
            parsed = parser.parse_args(rest)
            status = lane_queue.queue_row_status(parsed.id)
            print(_json.dumps(status, indent=2 if parsed.json else None, default=str))
            return 0
        st = dispatch.admission_state()
        if registered_cli_option_token('dispatch/--json') in rest:
            import json as _json
            print(_json.dumps(st, indent=2, default=str))
            return 0
        print("manage_all (universal admission): %s" % ("ON" if st.get("manage_all") else "OFF"))
        gov = st.get("governor") or {}
        print("governor — %d active key(s):" % len(gov))
        for k, b in gov.items():
            print("  %-26s limit=%s rpm=%s tpm=%s in_flight=%s waiting=%s"
                  % (k, b.get("limit"), b.get("rpm"), b.get("tpm"), b.get("in_flight"), b.get("waiting")))
        ll = st.get("learned_limits") or {}
        if ll:
            print("learned rate limits (self-calibrated from 429 + success headers):")
            for v, d in ll.items():
                print("  %-22s tpm=%s rpm=%s (%s)" % (v, d.get("tpm"), d.get("rpm"), d.get("source")))
        q = st.get("queue") or {}
        print("queue: pending=%s leased=%s parked=%s done=%s failed=%s retired=%s" % (
            q.get("pending"), q.get("leased"), q.get("parked"), q.get("done"), q.get("failed"), q.get("retired")))
        dc = st.get("deadline_cancels") or {}
        if dc:
            print("deadline-cancel waste (reasoning cut mid-thought — invisible to the ledger, reconcile to see $):")
            for m, n in dc.items():
                print("  %-26s x%s" % (m, n))
        ue = st.get("unhonored_efforts") or {}
        if ue:
            print("effort pins NOT honored (guardrail A — 'minimal' remapped to a floor that still reasons; no saving):")
            for k, n in ue.items():
                print("  %-26s x%s" % (k, n))
        rw = st.get("runaways") or {}
        if rw:
            print("per-call RUNAWAYS (guardrail E — out_tok >> the measured p99 norm; the loose ceiling never cut it):")
            for k, n in rw.items():
                print("  %-26s x%s" % (k, n))
        icr = st.get("intent_cap_refusals") or {}
        if icr:
            print("per-intent CAP refusals (guardrail D at the door — spend would cross caps.intent_caps):")
            for k, n in icr.items():
                print("  %-26s x%s" % (k, n))
        uf = st.get("ungated_fans") or {}
        if uf:
            print("UN-GATED fans (raw same-intent bursts NOT via bulk_delegate — route them through it):")
            for k, n in uf.items():
                print("  %-26s x%s" % (k, n))
        return 0


def cli_core_install_mcp(rest, cmd, argv):
        from . import mcp_server
        return mcp_server.register_client(remove=registered_cli_option_token('install-mcp/--remove') in rest)


def cli_core_price(rest, cmd, argv):
        from . import pricing as _pr
        r = list(rest)
        if not r or r[0].startswith("-"):
            print("usage: spendguard price <model> --in <$/1M> --out <$/1M> --source '<url or invoice>' "
                  "--verified YYYY-MM-DD "
                  "[--provider <name>] [--batch-in X] [--batch-out Y] [--cached-in Z]")
            return 2
        model = r[0]
        from .cli_arguments import cli_option_value_after
        try:
            path, entry = _pr.set_price(
                model, cli_option_value_after(r, registered_cli_option_token('price/--provider')), cli_option_value_after(r, registered_cli_option_token('price/--in')),
                cli_option_value_after(r, registered_cli_option_token('price/--out')), cli_option_value_after(r, registered_cli_option_token('price/--source'), ""),
                batch_in=cli_option_value_after(r, registered_cli_option_token('price/--batch-in')), batch_out=cli_option_value_after(r, registered_cli_option_token('price/--batch-out')),
                cached_in=cli_option_value_after(r, registered_cli_option_token('price/--cached-in')),
                verified=cli_option_value_after(r, registered_cli_option_token('price/--verified')))
        except ValueError as e:
            print(f"refused: {e}")
            return 2
        print(f"priced {model}: ${entry['in_']}/1M in · ${entry['out']}/1M out  (source: {entry['_source']})")
        print(f"  authored in {path}; the in-memory price table refreshes on the next read.")
        print(f"  Past UNPRICED rows for {model}")
        print("  can be re-costed with `spendguard reprice --model %s`." % model)
        return 0


def cli_core_reprice(rest, cmd, argv):
        from . import budget as _bud, config as _cfg
        r = list(rest)

        from .cli_arguments import cli_option_value_after
        _model, _apply = cli_option_value_after(r, registered_cli_option_token('reprice/--model')), (registered_cli_option_token('reprice/--apply') in r)
        if _apply:                                         # consistent snapshot BEFORE any write (integrity first)
            import sqlite3 as _sq
            import time as _t
            _db = _cfg.db_path()
            _bak = f"{_db}.bak_reprice_{int(_t.time())}"
            from .disk_guard import require_disk_space
            require_disk_space(_bak)
            _src = _sq.connect(_db)
            _dst = _sq.connect(_bak)
            with _dst:
                _src.backup(_dst)
            _dst.close()
            _src.close()
            print(f"snapshot: {_bak}")
        plan = _bud.reprice_unpriced(model=_model, apply=_apply)
        from collections import Counter as _Counter
        methods = _Counter(p["method"] for p in plan)
        total = sum((p["cost"] or 0.0) for p in plan if p["method"] != "skip")
        scope = f" for {_model}" if _model else ""
        print(f"reprice{scope}: {len(plan)} unpriced row(s) — {methods.get('update', 0)} repriced in place, "
              f"{methods.get('adjust', 0)} adjusted (locked period), {methods.get('skip', 0)} left unpriced "
              f"(still unresolvable).")
        print(f"  ${total:.4f} of previously-unpriced usage {'FOLDED INTO' if _apply else 'would fold into'} the total"
              + ("." if _apply else " — pass --apply to commit (audited; a closed period gets a delta)."))
        return 0


def cli_core_quarantine(rest, cmd, argv):
        from . import budget, pricing, config as _cfg
        rest_l = list(rest)
        _si = rest_l.index(registered_cli_option_token('quarantine/--since')) if registered_cli_option_token('quarantine/--since') in rest_l else -1   # `--since` as the LAST arg (no value) indexed
        since = rest_l[_si + 1] if 0 <= _si < len(rest_l) - 1 else _cfg.month_start_utc()   # past the end → IndexError
        if registered_cli_option_token('quarantine/--ts') in rest_l or registered_cli_option_token('quarantine/--row') in rest_l:
            # BOUNDS-CHECKED. `--row` as the FINAL argument indexed past the end and raised a bare
            # IndexError out of the CLI — a traceback where a usage message belongs, on the command that
            # EDITS THE LEDGER. _opt returns None for a flag given without a value, so the missing-target
            # check below can report it as the user error it is.
            from .cli_arguments import cli_option_value_after
            reason = cli_option_value_after(rest_l, registered_cli_option_token('quarantine/--reason')) or "impossible estimate"
            _row = cli_option_value_after(rest_l, registered_cli_option_token('quarantine/--row'))
            try:
                row = int(_row) if _row is not None else None
            except ValueError:
                print(f"quarantine: --row must be a rowid (got {_row!r})"); return 2
            ts = cli_option_value_after(rest_l, registered_cli_option_token('quarantine/--ts'))
            if row is None and ts is None:
                print("quarantine: --row <rowid> or --ts <timestamp> is required "
                      "(the flag was given without a value)"); return 2
            try:
                n = budget.quarantine_charge(ts=ts, reason=reason, row=row)
            except ValueError as e:                 # ambiguous timestamp — say so, never tag them all
                print(str(e))
                return 2
            target = f"row {row}" if row is not None else ts
            print(f"quarantined {n} row(s) at {target} — excluded from every total, kept for audit."
                  if n else f"no un-quarantined charge at {target}")
            return 0 if n else 1
        rows = budget.suspect_batches(since)
        if not rows:
            print(f"no batch charges since {since}")
            return 0
        print(f"batch charges since {since} — check the per-request arithmetic before quarantining:\n")
        print(f"  {'row':>6}  {'ts':<26}{'model':<20}{'cost':>10}  {'in_tok':>14}  {'ctx limit':>10}  caller")
        for r in rows:
            lim = pricing.max_input_tokens(r["model"])
            flag = " ←" if (lim and r["in_tok"] and r["in_tok"] > lim) else ""
            mark = " [quarantined]" if r["conv_id"] == budget.QUARANTINE_CONV else ""
            print(f"  {r['row']:>6}  {r['ts']:<26}{r['model']:<20}{r['cost']:>10,.2f}  {(r['in_tok'] or 0):>14,}  "
                  f"{(lim or 0):>10,}{flag}  {r['caller']}{mark}")
        print("\n  ← the batch's TOTAL input already exceeds one request's context window. Divide by the number"
              "\n    of requests it held: if that still exceeds the limit, the estimate was impossible."
              "\n    Quarantine it with:  spendguard quarantine --row <row> --reason '<why>'"
              "\n    (--row, not --ts: a timestamp can cover several charges, and tagging them all would be"
              "\n     a worse bug than the one being repaired.)")
        return 0


def cli_core_codex_gc(rest, cmd, argv):
        from . import codex_exec
        import argparse as _agc
        _p = _agc.ArgumentParser(prog="spendguard codex-gc")
        add_registered_cli_argument(_p, 'codex-gc/cli._dispatch/_p/--max-age-days', type=float)
        add_registered_cli_argument(_p, 'codex-gc/cli._dispatch/_p/--apply')
        _a = _p.parse_args(rest)
        r = codex_exec.gc_shell_snapshots(max_age_days=_a.max_age_days, apply=_a.apply)
        _mb = r["bytes"] / 1e6
        print(f"codex shell-snapshot gc — {r['dir']}")
        if r["error"]:
            print(f"  🔴 could not scan: {r['error']}")
            return 2
        print(f"  examined {r['examined']}, stale (>{r['cutoff_days']:g}d) {r['stale']} = {_mb:.1f} MB"
              + (f", skipped {r['skipped']}" if r["skipped"] else ""))
        if _a.apply:
            print(f"  DELETED {r['deleted']} file(s), freed ~{_mb:.1f} MB"
                  + (f" ({r['skipped']} could not be removed — see perms)" if r["skipped"] else ""))
        else:
            print(f"  DRY-RUN — re-run with --apply to delete the {r['stale']} stale file(s) (~{_mb:.1f} MB). "
                  f"Recent snapshots are never touched.")
            print("  Opt-in periodic cleanup: schedule `spendguard codex-gc --apply` (launchd/cron or `spendguard schedule`).")
        return 0


def cli_core_embed_gc(rest, cmd, argv):
        from . import adapters as _aegc
        import argparse as _aeg
        _p = _aeg.ArgumentParser(prog="spendguard embed-gc")
        add_registered_cli_argument(_p, 'embed-gc/cli._dispatch/_p/--max-age-days', type=float)
        add_registered_cli_argument(_p, 'embed-gc/cli._dispatch/_p/--max-total-gb', type=float)
        add_registered_cli_argument(_p, 'embed-gc/cli._dispatch/_p/--apply')
        _a = _p.parse_args(rest)
        r = _aegc.gc_embed_checkpoints(max_age_days=_a.max_age_days, max_total_gb=_a.max_total_gb, apply=_a.apply)
        _gb = r["bytes"] / (1024 ** 3)
        print(f"embed resume-checkpoint gc — {r['dir']}")
        if r["error"]:
            print(f"  🔴 could not scan: {r['error']}")
            return 2
        _doomed = r["stale"] + r["over_cap"]
        print(f"  examined {r['examined']}, cold (>{r['cutoff_days']:g}d) {r['stale']}, "
              f"over the {r['max_total_gb']:g} GB cap {r['over_cap']} → {_doomed} file(s) = {_gb:.2f} GiB"
              + (f", skipped {r['skipped']}" if r["skipped"] else ""))
        if _a.apply:
            print(f"  DELETED {r['deleted']} file(s), freed ~{_gb:.2f} GiB"
                  + (f" ({r['skipped']} could not be removed — see perms)" if r["skipped"] else ""))
        else:
            print(f"  DRY-RUN — re-run with --apply to delete the {_doomed} file(s) (~{_gb:.2f} GiB). "
                  f"A re-run of a corpus whose checkpoint is still here re-pays nothing, so recent ones are kept.")
            print("  embed() also prunes on create, so this is for a manual sweep / a one-off backlog.")
        return 0


def cli_core_cross_check(rest, cmd, argv):
        from . import pricing as p
        try:
            rows, matched, total = p.cross_check_openrouter()
        except Exception as e:
            print(f"cross-check failed (network?): {e}"); return 1
        print(f"price cross-check vs OpenRouter — {matched}/{total} models matched "
              f"(frontier models not on OpenRouter don't match; that's coverage, not error)")
        print(f"  {'model':<24}{'our in/out':>16}{'OR in/out':>16}  flag")
        for model, oi, ri, oo, ro, flag in rows:
            print(f"  {model[:23]:<24}{('$%.2f/$%.2f' % (oi, oo)):>16}{('$%.2f/$%.2f' % (ri, ro)):>16}  "
                  f"{'⚠️ ' + flag if flag == 'DRIFT' else flag}")
        if not rows:
            print("  (no overlapping models — your table is mostly frontier models OpenRouter doesn't list.)")
        return 0


def cli_core_savings(rest, cmd, argv):
        from . import guard as _g
        s, ds, shed = _g.saved_since(), _g.decisions_summary(), _g.shed_summary()
        if registered_cli_option_token('savings/--json') in rest:
            import json as _j
            print(_j.dumps({"saved": s, "decisions": ds, "shed": shed}, indent=2, default=str))
            return 0
        print("spendguard SAVINGS — a THIRD axis, kept SEPARATE from real-$ and est-value (never summed into either):")
        print(f"  certain (measured)     ${s.get('certain', 0):.2f}")
        print(f"  counterfactual (est)   ${s.get('counterfactual', 0):.2f}")
        print(f"  ── total guarded       ${s.get('total', 0):.2f}")
        _by = s.get("by_source") or {}
        if _by:
            print("  by source: " + "   ".join(f"{k} ${v:.2f}" for k, v in sorted(_by.items(), key=lambda kv: -kv[1])))
        print(f"  decisions booked: {ds.get('decisions', 0)}  (best-value / advisor swaps, each priced vs its counterfactual)")
        for row in (ds.get("by_intent") or [])[:8]:
            saved_value = (f"${row['saved_usd']:.2f}" if row.get("saved_usd") is not None else "unknown usage/value")
            print(f"    {str(row.get('intent', '?'))[:44]:44} {row.get('decisions', 0):>5} dec  {saved_value}")
        if shed.get("n_sheds"):
            print(f"\n  SHED — substitutable work routed OFF a capped/overage/shed lane ({shed['n_sheds']} sheds, "
                  f"{shed.get('served_free_n', 0)} served free · {shed.get('paid_n', 0)} paid):")
            avoided_value = (f"${shed['avoided_overage_usd']:.4f}" if shed.get("avoided_overage_usd") is not None else "unknown usage/value")
            preserved_value = (f"${shed['quota_preserved_usd']:.4f}" if shed.get("quota_preserved_usd") is not None else "unknown usage/value")
            print(f"    avoided overage  {avoided_value}  (REAL money — a plan on paid overage would have billed)")
            print(f"    quota preserved  {preserved_value}  (est-value moved to another free lane — NOT real money, separate axis)")
            for r in (shed.get("by_route") or [])[:6]:
                route_value = (f"${r['counterfactual_usd']:.4f}" if r["counterfactual_usd"] is not None else "unknown usage/value")
                print(f"    {str(r['from_lane'])[:14]:14} → {str(r['to_lane'])[:14]:14} {r['class']:9} "
                      f"{r['n']:>4} sheds  {route_value}")
        return 0


def cli_core_register_critical(rest, cmd, argv):
        import argparse
        from . import lane_balance
        ap = argparse.ArgumentParser(prog="spendguard register-critical")
        add_registered_cli_argument(ap, 'register-critical/cli._dispatch/ap/patterns')
        add_registered_cli_argument(ap, 'register-critical/cli._dispatch/ap/--source')
        add_registered_cli_argument(ap, 'register-critical/cli._dispatch/ap/--list')
        a = ap.parse_args(rest)
        cov = (lane_balance.bandit_list_coverage() or {}).get("bandit_denylist", {})
        if a.list or not a.patterns:
            src = cov.get("sources") or {}
            print("advisor.bandit_denylist — no-substitution pins (a cross-vendor panel is never collapsed to one vendor):")
            for e in (cov.get("entries") or []):
                print(f"  {e:<42} {('← ' + src[e]) if e in src else ''}")
            if not cov.get("entries"):
                print("  (none) — register with: spendguard register-critical <intent-pattern…> --source <name>")
            return 0
        merged = lane_balance.register_critical(a.patterns, source=a.source)
        print(f"registered {len(a.patterns)} pin(s){(' from ' + a.source) if a.source else ''} → "
              f"advisor.bandit_denylist ({len(merged)} total, deduped)")
        return 0


def cli_core_keys(rest, cmd, argv):
        from . import budget, config as _c
        since = None
        for i, a in enumerate(rest):
            if a == registered_cli_option_token('keys/--since') and i + 1 < len(rest):
                since = rest[i + 1]
        # `_dt` is the stdlib datetime module imported two lines up; `_dt.config` does not exist, so this
        # raised AttributeError every time --since was omitted — the DEFAULT path of the command.
        since = since or _c.month_start_utc()
        prof = _c._key_profile()
        print(f"per-key workload spend since {since}" + (f"  (active key profile: {prof})" if prof else ""))
        rows = sorted(budget.by_key(since=since).items(), key=lambda x: -x[1]["cost"])
        if not rows:
            print("  (no workload charges in the window)")
        for (prov, fp), v in rows:
            note = "  ← rows before key stamping / no key env resolved" if fp == "(none)" else ""
            print(f"  {prov:<11}{fp:<16}${v['cost']:>10.2f}  {v['calls']:>6} calls{note}")
        print("  (fingerprint = sha256[:8]:last4 of the serving key — local-only, never pushed)")
        return 0


def cli_core_check_prices(rest, cmd, argv):
        from . import pricing as p
        v, days, stale = p.freshness()
        flag = f"  ⚠️ STALE (>{p.STALE_AFTER_DAYS}d) — re-verify against the source below" if stale else "  (fresh)"
        print(f"prices verified {v} ({days} days ago){flag}")
        print(f"  source : {p.PRICING_SOURCE}")
        print("  config : spendguard price <model> --in <rate> --out <rate> --source <URL> --verified YYYY-MM-DD")
        for prov, models in sorted(p.providers().items()):
            print(f"  {prov}: {len(models)} models")
        from .ledger_read import LedgerReadError
        try:                                              # capability coverage/completeness pointer (full detail: `metadata`)
            from . import metadata_audit as _ma
            _cap = _ma._capability_health()
            if _cap.get("catalog_n"):
                print(f"  capabilities: {_cap['covered']}/{_cap['catalog_n']} models have LiteLLM data · "
                      f"{len(_cap.get('uncovered') or [])} uncovered · {_cap.get('discover_total', 0)} newer LiteLLM "
                      f"models to consider adding (`spendguard metadata` / `sync-capabilities --audit` for detail)")
        except LedgerReadError:
            raise
        except Exception:
            pass                                          # a capability-audit hiccup never fails the price-freshness view
        return 2 if stale else 0


def cli_core_version(rest, cmd, argv):
        from . import __version__, release as _rel
        _s = _rel.served_sha()
        _tag = (f" ({_s['describe']})" + ("  [dirty]" if _s.get("dirty") else "")) if _s else ""
        print(f"llm-spendguard {__version__}{_tag}")
        if _rel.stale_vs_green():                      # this process is behind the deployed green commit
            _g = _rel.green_pointer() or {}
            print(f"  ⚠ STALE vs green pointer {_g.get('short')} — `spendguard deploy` promotes; "
                  "MCP servers roll onto it on their next request. `spendguard release` for detail.")
        return 0




if __name__ == "__main__":
    sys.exit(main())
