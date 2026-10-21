"""Fast, offline PreToolUse guard for Claude Code weekly-cap spawn leaks."""
import datetime
import json
import os
import pathlib
import shlex
import shutil
import sys

from . import config
from .ledger_read import LedgerReadError


WEEKLY_CAP_GUARD_ENABLED_DEFAULT = True
WEEKLY_CAP_GUARD_THRESHOLD_DEFAULT = 0.30
WEEKLY_CAP_GUARD_ON_UNKNOWN_DEFAULT = "block"
WEEKLY_CAP_GUARD_BLOCKED_SPAWN_TOOLS_DEFAULT = ["Task", "Agent"]
WEEKLY_CAP_GUARD_OVERRIDE_ENV = "SPENDGUARD_CAPGUARD_ALLOW"
WEEKLY_CAP_GUARD_KILL_SWITCH_ENV = "GATE_DISABLE"
WEEKLY_CAP_GUARD_HOST_LANE = "claude-code"
WEEKLY_CAP_GUARD_BASH_TOOL = "Bash"
WEEKLY_CAP_GUARD_HOOK_SUFFIX = "capguard --pretooluse-hook"
WEEKLY_CAP_GUARD_HOOK_TIMEOUT_SECONDS = 30
WEEKLY_CAP_GUARD_HOOK_MATCHER = "*"
WEEKLY_CAP_GUARD_UNKNOWN_BLOCK = "block"
WEEKLY_CAP_GUARD_UNKNOWN_WARN = "warn"
WEEKLY_CAP_GUARD_CONFIG_SECTION = "advisor"
WEEKLY_CAP_GUARD_CONFIG_PREFIX = "weekly_cap_guard."
WEEKLY_CAP_GUARD_REMEDY_TEMPLATE = "spendguard delegate --provider codex --cwd {repo}"
WEEKLY_CAP_GUARD_DISABLE_GUIDANCE = "advisor.weekly_cap_guard.enabled=false"
WEEKLY_CAP_GUARD_DENY_TEMPLATE = (
    "spendguard weekly-cap guard denied this on-plan spawn: Anthropic weekly headroom {headroom}; "
    "block threshold {threshold}. Route through the warm off-plan lane with `{remedy}`. "
    "For this one call set {override}=1; disable with {disable}."
)
WEEKLY_CAP_GUARD_OVERRIDE_TEMPLATE = (
    "[spendguard] weekly-cap override {override}=1: allowing guarded spawn at {headroom}."
)
WEEKLY_CAP_GUARD_CRASH_TEMPLATE = "[spendguard] cap-guard internal error; allowing tool call: {error}"
WEEKLY_CAP_GUARD_SURFACE_TEMPLATE = (
    "[spendguard] anthropic weekly: {percent}% left · resets {reset} · cap-guard {mode}"
)
WEEKLY_CAP_GUARD_UNKNOWN_TEMPLATE = "[spendguard] anthropic weekly: unknown · cap-guard {mode}"


def _weekly_cap_guard_setting(key, default):
    return config._cfg_get(WEEKLY_CAP_GUARD_CONFIG_SECTION, WEEKLY_CAP_GUARD_CONFIG_PREFIX + key, default)


def anthropic_weekly_headroom():
    """Return (fraction-left, reset timestamp), or (None, None), from lanes.lane_headroom's provider-truth SSOT."""
    from . import lanes
    row = next((item for item in lanes.lane_headroom(do_fetch=False)
                if item.get("lane") == WEEKLY_CAP_GUARD_HOST_LANE), None)
    if not row or not row.get("known") or row.get("remaining_pct") is None:
        return None, None
    return float(row["remaining_pct"]) / 100.0, row.get("reset_ts")


def _weekly_cap_guard_result(decision, message):
    return {"systemMessage": message, "hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision,
        "permissionDecisionReason": message}}


def command_invokes_raw_codex_exec(command):
    """True when a shell command position invokes a basename `codex` followed by `exec`.

    Quoted text passed to another command is not a command position; shell control operators start a new position.
    This intentionally recognizes direct invocations, not semantic mentions of the words.
    """
    try:
        lexer = shlex.shlex(str(command), posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except (TypeError, ValueError):
        return False
    at_command = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and all(char in ";&|()" for char in token):
            at_command = True
            index += 1
            continue
        if at_command:
            if "=" in token and not token.startswith(("/", "./", "../")):
                index += 1
                continue
            if token == "env":
                index += 1
                continue
            at_command = False
            if pathlib.PurePath(token).name == "codex":
                if index + 1 < len(tokens) and tokens[index + 1] == "exec":
                    return True
        index += 1
    return False


def _weekly_cap_guard_spawn(payload, blocked_tools):
    tool_name = str(payload.get("tool_name") or "")
    if tool_name in blocked_tools:
        return True
    tool_input = payload.get("tool_input") or {}
    return tool_name == WEEKLY_CAP_GUARD_BASH_TOOL and command_invokes_raw_codex_exec(tool_input.get("command") or "")


def _weekly_cap_guard_headroom_text(frac_left):
    return "unknown" if frac_left is None else f"{frac_left * 100:.0f}% left"


def _weekly_cap_guard_reset_text(reset_ts):
    if not reset_ts:
        return "unknown"
    return datetime.datetime.fromtimestamp(float(reset_ts)).strftime("%b %d %H:%M")


def weekly_cap_guard_pretooluse(payload):
    """Return the Claude Code PreToolUse decision. Deliberate policy denies closed; internal failures allow open."""
    try:
        enabled = bool(_weekly_cap_guard_setting("enabled", WEEKLY_CAP_GUARD_ENABLED_DEFAULT))
        blocked_tools = list(_weekly_cap_guard_setting(
            "blocked_spawn_tools", WEEKLY_CAP_GUARD_BLOCKED_SPAWN_TOOLS_DEFAULT))
        blockable = _weekly_cap_guard_spawn(payload, blocked_tools)
        if not blockable:
            return _weekly_cap_guard_result("allow", "spendguard cap-guard: tool is outside guarded spawn shapes")
        if not enabled or config.disabled():
            return _weekly_cap_guard_result("allow", "spendguard cap-guard disabled")
        threshold = float(_weekly_cap_guard_setting(
            "block_below_frac_left", WEEKLY_CAP_GUARD_THRESHOLD_DEFAULT))
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("weekly cap threshold must be between 0 and 1")
        on_unknown = str(_weekly_cap_guard_setting("on_unknown", WEEKLY_CAP_GUARD_ON_UNKNOWN_DEFAULT))
        if on_unknown not in (WEEKLY_CAP_GUARD_UNKNOWN_BLOCK, WEEKLY_CAP_GUARD_UNKNOWN_WARN):
            raise ValueError("weekly cap unknown policy must be block or warn")
        frac_left, reset_ts = anthropic_weekly_headroom()
        armed = (frac_left is not None and frac_left < threshold
                 or frac_left is None and on_unknown == WEEKLY_CAP_GUARD_UNKNOWN_BLOCK)
        mode = "armed" if armed else "warn"
        if frac_left is None:
            print(WEEKLY_CAP_GUARD_UNKNOWN_TEMPLATE.format(mode=mode), file=sys.stderr)
        else:
            print(WEEKLY_CAP_GUARD_SURFACE_TEMPLATE.format(
                percent=f"{frac_left * 100:.0f}", reset=_weekly_cap_guard_reset_text(reset_ts), mode=mode),
                file=sys.stderr)
        headroom = _weekly_cap_guard_headroom_text(frac_left)
        if os.getenv(WEEKLY_CAP_GUARD_OVERRIDE_ENV) == "1":
            warning = WEEKLY_CAP_GUARD_OVERRIDE_TEMPLATE.format(
                override=WEEKLY_CAP_GUARD_OVERRIDE_ENV, headroom=headroom)
            print(warning, file=sys.stderr)
            return _weekly_cap_guard_result("allow", warning)
        if armed:
            repo = str(payload.get("cwd") or pathlib.Path.cwd())
            message = WEEKLY_CAP_GUARD_DENY_TEMPLATE.format(
                headroom=headroom, threshold=f"{threshold:.0%}",
                remedy=WEEKLY_CAP_GUARD_REMEDY_TEMPLATE.format(repo=shlex.quote(repo)),
                override=WEEKLY_CAP_GUARD_OVERRIDE_ENV, disable=WEEKLY_CAP_GUARD_DISABLE_GUIDANCE)
            return _weekly_cap_guard_result("deny", message)
        return _weekly_cap_guard_result("allow", "spendguard cap-guard: weekly headroom permits this spawn")
    except LedgerReadError as exc:
        return _weekly_cap_guard_result("deny", f"spendguard ledger unavailable; refusing spawn: {exc}")
    except Exception as exc:
        warning = WEEKLY_CAP_GUARD_CRASH_TEMPLATE.format(error=f"{type(exc).__name__}: {str(exc)[:160]}")
        print(warning, file=sys.stderr)
        return _weekly_cap_guard_result("allow", warning)


def cmd(argv=None):
    if "--pretooluse-hook" not in list(argv or []):
        print("usage: spendguard capguard --pretooluse-hook", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = weekly_cap_guard_pretooluse(payload)
    except LedgerReadError as exc:
        result = _weekly_cap_guard_result("deny", f"spendguard ledger unavailable; refusing spawn: {exc}")
    except Exception as exc:
        warning = WEEKLY_CAP_GUARD_CRASH_TEMPLATE.format(error=f"{type(exc).__name__}: {str(exc)[:160]}")
        print(warning, file=sys.stderr)
        result = _weekly_cap_guard_result("allow", warning)
    print(json.dumps(result))
    return 0


def _weekly_cap_guard_settings_path():
    return pathlib.Path.home() / ".claude" / "settings.json"


def _weekly_cap_owned_hook(command):
    return command.endswith(WEEKLY_CAP_GUARD_HOOK_SUFFIX)


def install_weekly_cap_guard(settings_path=None, uninstall=False, executable=None):
    """Idempotently merge or remove only spendguard's weekly-cap PreToolUse hook."""
    path = pathlib.Path(settings_path) if settings_path else _weekly_cap_guard_settings_path()
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except Exception as exc:
            raise RuntimeError(f"{path} is unparseable; refusing to write ({exc})") from exc
    else:
        settings = {}
    hooks = settings.get("hooks") or {}
    groups = list(hooks.get("PreToolUse") or [])
    found = any(_weekly_cap_owned_hook(hook.get("command", ""))
                for group in groups for hook in (group.get("hooks") or []))
    if uninstall:
        if not found:
            return "already absent", None
        revised = []
        for group in groups:
            kept = [hook for hook in (group.get("hooks") or [])
                    if not _weekly_cap_owned_hook(hook.get("command", ""))]
            if kept:
                revised.append({**group, "hooks": kept})
        if revised:
            hooks["PreToolUse"] = revised
        else:
            hooks.pop("PreToolUse", None)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        action = "uninstalled"
    else:
        if found:
            return "already installed", None
        binary = executable or shutil.which("spendguard") or str(pathlib.Path(sys.prefix) / "bin" / "spendguard")
        command = f"env SPENDGUARD_NO_AUTOINSTALL=1 {shlex.quote(binary)} {WEEKLY_CAP_GUARD_HOOK_SUFFIX}"
        groups.append({"matcher": WEEKLY_CAP_GUARD_HOOK_MATCHER, "hooks": [
            {"type": "command", "command": command, "timeout": WEEKLY_CAP_GUARD_HOOK_TIMEOUT_SECONDS}]})
        hooks["PreToolUse"] = groups
        settings["hooks"] = hooks
        action = "installed"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".capguard.bak")
    if path.exists():
        shutil.copy2(path, backup)
    config.update_json(path, lambda _old: settings, reason="install-weekly-cap-guard", required=True)
    return action, (backup if backup.exists() else None)


def install_weekly_cap_guard_cli(argv=None):
    args = list(argv or [])
    if any(arg != "--uninstall" for arg in args):
        print("usage: spendguard install-capguard [--uninstall]", file=sys.stderr)
        return 2
    action, backup = install_weekly_cap_guard(uninstall="--uninstall" in args)
    print(f"{action} spendguard weekly-cap guard → {_weekly_cap_guard_settings_path()}"
          + (f"  ·  backup: {backup}" if backup else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd())
