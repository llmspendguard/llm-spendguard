"""Inline spend receipts — make what spendguard tracked visible AS IT HAPPENS, plus the running tally.

After every gated FLOW (a `with spendguard.context(...)` block, a batch submit, or a CLI command) we emit one
compact summary: what ran, input/output tokens, estimate → actual cost, and the running daily/weekly/monthly tally.
Per-FLOW, never per-call — a chat loop fires hundreds of tiny calls and a receipt each would be noise; we aggregate
at the flow boundary (see `_call_corpus_accessors.resolve_calls_context`).

TWO AXES, NEVER SUMMED (the invariant the whole system enforces):
  • ACTUAL-$   — billed workload (batch + realtime + GPU). Reconciles to provider truth. Source: the gate ledger
                 (`budget.resolve_spent_since`) — always-on, no admin key, zero LLM.
  • EST-VALUE  — Claude Code / claude.ai usage value: what it WOULD cost at API rates, NOT money billed. Source:
                 the ledger's est_chat_usd axis; cached transcript miners cover sources without ledger rows,
                 with an "as-of" date so their freshness stays visible.

Verbosity (config `receipts` / env `SPENDGUARD_RECEIPTS`): off | footer | flow | verbose.
  off     — emit nothing.
  footer  — only the running tally (used by the CLI / the Claude Code Stop hook).
  flow    — a per-flow block + the tally (default).
  verbose — flow + tally + a learned tip + the caller.

Auto-emit (flow boundaries) goes to STDERR so it never corrupts a script's stdout / piped data. The explicit
`spendguard receipt` command prints to STDOUT — that's what the Claude Code Stop hook captures to surface in-chat.
Cost: a local sqlite read + a small JSON read. Safe to emit always; it never spends and never needs a key.
"""
from __future__ import annotations

from ...capability_cli_parsing import registered_cli_option_token

import json
import logging
import os
import pathlib
import sys

from ... import config
from ...sources import CLAUDE_CODE_SOURCE, CODEX_SOURCE

from .receipt_tally import (
    AT_CEILING as AT_CEILING,
    LEVELS as LEVELS,
    NO_CAP as NO_CAP,
    RAISABLE as RAISABLE,
    ReceiptPlanAllocationRefused as ReceiptPlanAllocationRefused,
    _BY_DAY_KEEP as _BY_DAY_KEEP,
    _CACHE as _CACHE,
    _DEFAULT_PLANS as _DEFAULT_PLANS,
    _SOURCE_REFRESH as _SOURCE_REFRESH,
    _asof_label as _asof_label,
    _basis_line as _basis_line,
    _est_by_source as _est_by_source,
    _est_cells as _est_cells,
    _est_tally as _est_tally,
    _est_tree as _est_tree,
    _external_tally as _external_tally,
    _lane_activity as _lane_activity,
    _money as _money,
    _pct as _pct,
    _plan_label as _plan_label,
    _plan_usd as _plan_usd,
    _quarantine_lines as _quarantine_lines,
    _receipt_aggregate_cache_path as _receipt_aggregate_cache_path,
    _receipt_est_records as _receipt_est_records,
    _remote_tally as _remote_tally,
    _rewindow as _rewindow,
    _routed_lane_value as _routed_lane_value,
    _tok as _tok,
    _truncated_lines as _truncated_lines,
    _truncation_rows as _truncation_rows,
    _unpriced_lines as _unpriced_lines,
    _utc_today as _utc_today,
    _windows as _windows,
    level as level,
    stamp_est_value as stamp_est_value,
    stamp_remote as stamp_remote,
    tally as tally,
    truncation_remedy as truncation_remedy,
)


# ── rendering ─────────────────────────────────────────────────────────────--
from .receipt_render import (
    _PREFIX as _PREFIX,
    _INDENT as _INDENT,
    _gate_blocks_line as _gate_blocks_line,
    _lane_seg as _lane_seg,
    _queue_seg as _queue_seg,
    _tally_lines as _tally_lines,
    _cache_split_line as _cache_split_line,
    _lane_health_alert as _lane_health_alert,
    render_tally as render_tally,
    _project_for_cwd as _project_for_cwd,
    _k as _k,
    render_line as render_line,
    _conv_repos as _conv_repos,
    _all_repos as _all_repos,
    _sum_repos as _sum_repos,
    _month_total as _month_total,
    _est_breakdown as _est_breakdown,
    _breakdown_line as _breakdown_line,
    _render_scope as _render_scope,
    _conv_org as _conv_org,
    _two_axis_table as _two_axis_table,
    _saved_lines as _saved_lines,
    render_tree as render_tree,
    render_flow as render_flow,
    _learned_tip as _learned_tip,
)

from .receipt_emit import (
    _sinks as _sinks,
    _out as _out,
    _emit_contract_line as _emit_contract_line,
    emit_flow as emit_flow,
    _STATUSLINE_CACHE_S as _STATUSLINE_CACHE_S,
    _statusline_cache_path as _statusline_cache_path,
    _write_statusline_cache as _write_statusline_cache,
    _cached_tally_line as _cached_tally_line,
    _compaction_hint as _compaction_hint,
    _RENUDGE_GROWTH_FRAC as _RENUDGE_GROWTH_FRAC,
    _nudge_state_path as _nudge_state_path,
    _nudge_should_fire as _nudge_should_fire,
    _compaction_nudge as _compaction_nudge,
)


# The FAST status line — a standalone script the installer drops at HOME/statusline.py and points Claude Code's
# statusLine at. It reads the cached tally + the session prefix and prints, with NO spendguard import, so it renders
# in ~50ms instead of ~1s (the spendguard package import). The cache is kept fresh by `receipt --stop-hook` each turn.
_STATUSLINE_SCRIPT = r'''#!/usr/bin/env python3
"""spendguard fast status line — reads the cached tally + session prefix, prints. NO spendguard import (~50ms)."""
import sys, os, json
HOME = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
info = {}
try:
    if not sys.stdin.isatty():
        info = json.loads(sys.stdin.read() or "{}")
except Exception:
    info = {}
bits = []
cwd = (info.get("workspace") or {}).get("current_dir") or info.get("cwd") or ""
if cwd:
    bits.append(os.path.basename(str(cwd).rstrip("/")))
model = (info.get("model") or {}).get("display_name")
if model:
    bits.append(str(model))
cw = info.get("context_window") or info.get("contextWindow") or {}
pct = cw.get("used_percentage") if isinstance(cw, dict) else None
if isinstance(pct, (int, float)):
    bits.append("%.0f%% ctx" % float(pct))
prefix = "  ·  ".join(bits)
line = ""
try:
    with open(os.path.join(HOME, "statusline_tally.cache"), encoding="utf-8") as fh:
        line = fh.read().strip()
except Exception:
    line = ""
nudge = ""
try:
    tp = info.get("transcript_path") or info.get("transcriptPath")
    if tp and os.path.exists(tp):
        with open(tp, "rb") as f:
            f.seek(0, 2); sz = f.tell(); f.seek(max(0, sz - 65536))
            tail = f.read().decode("utf-8", "ignore")
        ctx = 0
        for ln in reversed(tail.splitlines()):
            if '"usage"' not in ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            u = ((o.get("message") or {}).get("usage")) or {}
            if u:
                ctx = int(u.get("input_tokens",0) or 0)+int(u.get("cache_read_input_tokens",0) or 0)+int(u.get("cache_creation_input_tokens",0) or 0)
                break
        if ctx:
            hint = {}
            try:
                with open(os.path.join(HOME, "compaction_hint.json")) as hf:
                    hint = json.load(hf)
            except Exception:
                hint = {}
            if ctx >= int(hint.get("threshold_tokens") or 100000):
                nudge = "  ·  ⚠ %dK tok/turn · /compact (guided)" % (ctx // 1000)   # no savings multiplier (see receipt.py)
except Exception:
    nudge = ""
sys.stdout.write((prefix + "  ·  " if prefix else "") + line + nudge + "\n")
'''


def _by_intent_report(args) -> int:
    """`spendguard receipt --by-intent [--today|--week|--all|--since YYYY-MM-DD] [--json]` — billed LLM $ grouped by
    the forensic intent label, sorted by cost. The caller had to query spend.db by hand to see this; now it is a
    command. The Σ over intents reconciles EXACTLY with the receipt's real-$ API line for the same window (budget.
    by_intent reuses spent_since's countable filter). Default window: month-to-date (the headline window)."""
    since = args[args.index(registered_cli_option_token('receipt/--since')) + 1] if registered_cli_option_token('receipt/--since') in args and args.index(registered_cli_option_token('receipt/--since')) + 1 < len(args) else None
    return render_intent_spend_receipt(today_only=registered_cli_option_token('receipt/--today') in args, week_only=registered_cli_option_token('receipt/--week') in args,
                                       all_time=registered_cli_option_token('receipt/--all') in args, since_override=since, as_json=registered_cli_option_token('receipt/--json') in args)


def render_intent_spend_receipt(*, today_only: bool = False, week_only: bool = False,
                                all_time: bool = False, since_override: str | None = None,
                                as_json: bool = False) -> int:
    """Render the existing intent totals using the exact countable receipt filter."""
    import spendguard.manage.admission.admission_accessors as budget
    today, week, month = _windows()
    since, wlabel = month, "month-to-date"
    if today_only:
        since, wlabel = today, "today"
    elif week_only:
        since, wlabel = week, "rolling 7d"
    elif all_time:
        since, wlabel = None, "all time"
    if since_override is not None:
        since = since_override
        wlabel = "since " + since
    rows = budget.resolve_by_intent(since=since)
    items = sorted(rows.items(), key=lambda kv: -kv[1]["cost"])
    total = sum(v["cost"] for _, v in items)
    calls = sum(v["calls"] for _, v in items)
    if as_json:
        print(json.dumps({"window": wlabel, "since": since, "total_usd": total, "calls": calls,
                          "by_intent": dict(items)}, indent=2))
        return 0
    print(f"billed LLM $ by intent · {wlabel}  (Σ reconciles with the receipt's API line)")
    if not items:
        print("  (no countable LLM spend in this window)")
        return 0
    w = max((len(k) for k, _ in items), default=6)
    for name, v in items:
        print(f"  {name:<{w}}  ${v['cost']:>11,.4f}  · {v['calls']:>7,} call{'' if v['calls'] == 1 else 's'}")
    print(f"  {'TOTAL':<{w}}  ${total:>11,.4f}  · {calls:>7,} calls")
    return 0


def cli(args) -> int:
    """`spendguard receipt [--footer|--flow|--json|--by-intent]` → prints the running tally to STDOUT (default
    --footer). This is what the Claude Code Stop hook runs to surface the tally in-chat; also handy to check the tally
    any time. `--by-intent` instead breaks billed LLM $ down by the forensic intent label."""
    args = list(args or [])
    if registered_cli_option_token('receipt/--statusline') not in args:                # the statusline path refreshes lazily inside _cached_tally_line (on a
        try:                                       # cache MISS only), so a cached repaint does NO refresh; every other
            from ... import lane_value               # receipt path keeps refreshing up front exactly as before
            lane_value.refresh_lane_value_if_stale()
        except Exception:
            pass

    def _arg(flag):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    def _cur_project():
        try:
            import spendguard.manage.admission.admission_accessors as budget
            return budget.resolve__project() or None
        except Exception:
            return None

    if registered_cli_option_token('receipt/--by-intent') in args:        # a MANUAL breakdown — handled BEFORE the hook-safe try (a real error should
        return _by_intent_report(args)   # surface here, not be swallowed into the silent return-0 a status line needs)

    try:
        if registered_cli_option_token('receipt/--json') in args:
            print(json.dumps({"tally": tally(), "tree": _est_tree(None)}, indent=2))
            return 0
        if registered_cli_option_token('receipt/--stop-hook') in args:
            # Claude Code Stop hook: `systemMessage` is the ONLY hook output the user sees — the global tally line,
            # PLUS, when the CURRENT session is bloated, the ready-to-paste EFFECTIVE /compact prompt to run.
            info = {}
            if not sys.stdin.isatty():
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            _line = render_line(tally())
            _write_statusline_cache(_line)          # refresh the FAST status line's cache each turn (cheap render reads it)
            _compaction_hint()                      # refresh the compaction nudge's account-level hint (self-caches hourly)
            _msg = _line
            _hook_context = None
            try:
                from ... import config as _cfg, compaction
                from ... import claudecode as _cc_residency
                _sid = info.get("session_id") or info.get("sessionId")
                _codex_host = os.getenv("SPENDGUARD_HOOK_HOST") == "codex"
                if _codex_host:
                    from ...codex import codex_hook_context_trajectory
                    try:
                        _trajectory = codex_hook_context_trajectory(info)
                        _res_lines = [f"Codex residency: {_trajectory['current']:,} current input tokens; "
                                      f"{_trajectory['turns']} turns ({_trajectory['model']})."]
                    except (OSError, ValueError) as _residency_error:
                        _res_lines = [f"Codex residency unavailable: {_residency_error}"]
                else:
                    _res_lines = _cc_residency.residency_summary_lines(_sid)
                if _res_lines:
                    _msg += "\n" + "\n".join(_res_lines)
                _nud = None if _codex_host else _compaction_nudge(info, str(_cfg.HOME), once=True)
                if _nud:
                    _compact = compaction.compact_snippet()
                    _msg = _line + "\n" + _nud + "\n→ paste to compact well:  " + _compact
                    _hook_context = _nud + "\nProactive compaction action: " + _compact
            except Exception:
                pass
            _output = {"systemMessage": _msg}
            if _hook_context:
                _output["hookSpecificOutput"] = {"hookEventName": "Stop", "additionalContext": _hook_context}
            print(json.dumps(_output))
            return 0
        if registered_cli_option_token('receipt/--precompact-hook') in args:
            # Claude Code PreCompact cannot inject context. Record the event + digest for SessionStart(compact), then
            # emit only a schema-valid, silent acknowledgement.
            info = {}
            if not sys.stdin.isatty():
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            from ... import compaction
            compaction.record_precompact(info)
            print(json.dumps({"suppressOutput": True}))
            return 0
        if registered_cli_option_token('receipt/--sessionstart-hook') in args:
            # Claude Code SessionStart(compact): measure post-context and re-inject the WHOLE digest that PreCompact
            # persisted. Other SessionStart sources receive no directive.
            info = {}
            if not sys.stdin.isatty():
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            from ... import compaction
            compaction.record_sessionstart(info)
            if info.get("source") == "compact":
                digest = compaction.preservation_digest(info)
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                    "additionalContext": "POST-COMPACTION PRESERVATION PINS — still in force:\n" + digest}}))
            return 0
        if registered_cli_option_token('receipt/--statusline') in args:
            # Claude Code statusLine: session JSON on stdin; prepend cwd · model · ctx% to the global one-line tally.
            info = {}
            if not sys.stdin.isatty():           # guard: never block when run manually in a terminal
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            cwd = (info.get("workspace") or {}).get("current_dir") or info.get("cwd") or ""
            bits = []
            if cwd:
                bits.append(os.path.basename(str(cwd).rstrip("/")))
            model = (info.get("model") or {}).get("display_name")
            if model:
                bits.append(str(model))
            cw = info.get("context_window") or info.get("contextWindow") or {}
            pct = cw.get("used_percentage") if isinstance(cw, dict) else None
            if pct is not None:
                try:
                    bits.append(f"{float(pct):.0f}% ctx")
                except Exception:
                    pass
            prefix = "  ·  ".join(bits)
            from ... import config as _cfg
            _nudge = _compaction_nudge(info, str(_cfg.HOME))
            print((prefix + "  ·  " if prefix else "") + _cached_tally_line() + ("  ·  " + _nudge if _nudge else ""))
            return 0
        if registered_cli_option_token('receipt/--line') in args:                          # compact one-line global tally (scripting)
            print(render_line(tally()))
            return 0
        # the ORG → TEAM → PROJECT tree: --all = every org · --org X = one org · default = the connection's org,
        # falling back to ALL when that org has no attributed value (its taxonomy org may differ from the conn org).
        scope = None if registered_cli_option_token('receipt/--all') in args else (_arg(registered_cli_option_token('receipt/--org')) if registered_cli_option_token('receipt/--org') in args else _conv_org())
        if scope and not _est_tree(scope):
            scope = None
        print(render_tree(scope_org=scope, project=None if registered_cli_option_token('receipt/--all') in args else _cur_project()))
        from ... import claudecode as _cc_residency
        print("\nRESIDENCY (latest Claude Code session)")
        for _res_line in _cc_residency.residency_summary_lines():
            print("  " + _res_line)
        return 0
    except ReceiptPlanAllocationRefused as allocation_error:
        logging.getLogger(__name__).exception("Contextual receipt allocation refused: %r", allocation_error)
        raise
    except Exception:
        # A status line / hook must NEVER break the caller. Emit nothing rather than an error.
        return 0


# ── host installers — manage WHERE the always-on tally surfaces (reproducible, removable) ─────────
def _spendguard_bin():
    import shutil
    return shutil.which("spendguard") or str(pathlib.Path(sys.prefix) / "bin" / "spendguard")


def _install_claude_code(remove=False):
    import shutil
    sg = _spendguard_bin()
    p = pathlib.Path.home() / ".claude" / "settings.json"
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text())
        except Exception:
            cfg = {}
        shutil.copy(p, p.with_suffix(".json.bak"))      # reversible
    if remove:
        _sl = (cfg.get("statusLine") or {}).get("command", "")
        if _sl.endswith("receipt --statusline") or _sl.endswith("statusline.py"):
            cfg.pop("statusLine", None)
        hooks = cfg.get("hooks") or {}
        # Remove ONLY our hook from each Stop group, keeping the group's OTHER hooks. The old filter dropped any
        # whole GROUP that contained our command — deleting unrelated hooks a user had placed in the same group.
        stop = []
        for g in (hooks.get("Stop") or []):
            kept = [h for h in (g.get("hooks") or []) if not h.get("command", "").endswith("receipt --stop-hook")]
            if kept:                                    # a group left with other hooks survives (with ours removed);
                stop.append({**g, "hooks": kept})       # a group that held ONLY our hook is dropped
        if "Stop" in hooks:
            if stop:
                hooks["Stop"] = stop
            else:
                hooks.pop("Stop", None)
        # remove ONLY our PreCompact / SessionStart hook from each group, keeping any others the user placed there
        for _evt, _suffix in (("PreCompact", "receipt --precompact-hook"),
                              ("SessionStart", "receipt --sessionstart-hook")):
            groups = []
            for g in (hooks.get(_evt) or []):
                kept = [h for h in (g.get("hooks") or []) if not h.get("command", "").endswith(_suffix)]
                if kept:
                    groups.append({**g, "hooks": kept})
            if _evt in hooks:
                if groups:
                    hooks[_evt] = groups
                else:
                    hooks.pop(_evt, None)
        if not hooks:
            cfg.pop("hooks", None)
        action = "removed"
    else:
        # FAST status line: drop a standalone no-import script and point statusLine at it (~50ms vs the ~1s spendguard
        # package import per repaint). Its cache is refreshed every turn by the --stop-hook below; with no cache yet it
        # degrades to just the session prefix. Falls back to the heavy `receipt --statusline` only if the drop fails.
        _script = config.HOME / "statusline.py"
        try:
            config.HOME.mkdir(parents=True, exist_ok=True)
            _script.write_text(_STATUSLINE_SCRIPT)
            _cmd = f"{sys.executable} {_script}"
        except Exception:
            _cmd = f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --statusline"
        cfg["statusLine"] = {"type": "command", "command": _cmd, "padding": 0}
        stop = cfg.setdefault("hooks", {}).setdefault("Stop", [])
        if not any(h.get("command", "").endswith("receipt --stop-hook")
                   for g in stop for h in (g.get("hooks") or [])):
            stop.append({"hooks": [{"type": "command",
                                    "command": f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --stop-hook",
                                    "timeout": 5}]})
        # PreCompact: inject the preservation guidance (cuts the measured 19% auto-compaction loss) + record the
        # event. Added ALONGSIDE any existing PreCompact hooks (e.g. ccwatch's), never replacing them.
        pre = cfg.setdefault("hooks", {}).setdefault("PreCompact", [])
        if not any(h.get("command", "").endswith("receipt --precompact-hook")
                   for g in pre for h in (g.get("hooks") or [])):
            pre.append({"hooks": [{"type": "command",
                                   "command": f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --precompact-hook",
                                   "timeout": 10}]})
        # SessionStart(compact): record the post-compaction context so the real per-event k× (pre/post) is measured.
        ss = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])
        if not any(h.get("command", "").endswith("receipt --sessionstart-hook")
                   for g in ss for h in (g.get("hooks") or [])):
            ss.append({"matcher": "compact", "hooks": [{"type": "command",
                       "command": f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --sessionstart-hook", "timeout": 5}]})
        action = "installed"
    p.parent.mkdir(parents=True, exist_ok=True)
    config.update_json(p, lambda _d: cfg)
    # The residency nudge is another Claude Code receipt/context surface. Install it through this same host
    # integration entrypoint so one reversible command owns all of spendguard's Claude Code hooks.
    from spendguard.manage.admission.admission_accessors import resolve_install_residency_gate as install_residency_gate
    install_residency_gate(settings_path=p, uninstall=remove, executable=sg)
    print(f"{action} spendguard receipts for Claude Code → {p}")
    print("  restart Claude Code to apply" + ("" if remove else "  (status-line footer + per-turn notice)")
          + (f"  ·  backup: {p.with_suffix('.json.bak').name}" if p.with_suffix('.json.bak').exists() else ""))
    return 0


def install_cli(args):
    """`spendguard install-receipts [--host claude-code|codex] [--remove]` — manage the always-on in-chat tally."""
    args = list(args or [])
    host = CLAUDE_CODE_SOURCE
    if registered_cli_option_token('install-receipts/--host') in args:
        try:
            host = args[args.index(registered_cli_option_token('install-receipts/--host')) + 1]
        except IndexError:
            pass
    remove = registered_cli_option_token('install-receipts/--remove') in args
    if host in (CLAUDE_CODE_SOURCE, "claude", "cc"):
        return _install_claude_code(remove=remove)
    if host == CODEX_SOURCE:
        # Codex has no in-chat hook: its `notify` runs a program but does NOT render output in the transcript
        # (and is usually already taken). Use the inline receipt + a file sink any pane/editor can show.
        print("Codex has no Claude-Code-style in-chat hook. spendguard already TRACKS Codex (channel=codex, "
              "billed=false → est-value) via `spendguard codex show`. To surface the tally, use a file sink:")
        print("  spendguard config set receipts.sinks 'stderr,file:~/.spendguard/receipt.log'")
        print("then tail/show ~/.spendguard/receipt.log in a pane or editor. `spendguard receipt` prints it on demand.")
        return 0
    print(f"unknown host '{host}' (supported: claude-code | codex)")
    return 1


from . import spend_reporting_accessors as _spend_reporting_accessors

_spend_reporting_accessors.set__money_resolver(lambda *args, **kwargs: _money(*args, **kwargs))
_spend_reporting_accessors.set__plan_usd_resolver(lambda *args, **kwargs: _plan_usd(*args, **kwargs))
_spend_reporting_accessors.set__receipt_aggregate_cache_path_resolver(
    lambda *args, **kwargs: _receipt_aggregate_cache_path(*args, **kwargs))
_spend_reporting_accessors.set__rewindow_resolver(lambda *args, **kwargs: _rewindow(*args, **kwargs))
_spend_reporting_accessors.set__utc_today_resolver(lambda *args, **kwargs: _utc_today(*args, **kwargs))
_spend_reporting_accessors.set__windows_resolver(lambda *args, **kwargs: _windows(*args, **kwargs))
_spend_reporting_accessors.set_stamp_est_value_resolver(lambda *args, **kwargs: stamp_est_value(*args, **kwargs))
_spend_reporting_accessors.set_stamp_remote_resolver(lambda *args, **kwargs: stamp_remote(*args, **kwargs))


if __name__ == "__main__":      # `python -m spendguard.receipt [--all|--org X|--line|--stop-hook|--statusline]`
    raise SystemExit(cli(sys.argv[1:]))
