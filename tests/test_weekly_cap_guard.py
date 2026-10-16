"""Offline contract for the weekly-cap PreToolUse guard and its settings merger."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile

_home = tempfile.mkdtemp(prefix="sg-weekly-cap-guard-")
os.environ["SPENDGUARD_HOME"] = _home
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "sk-test-weekly-cap-guard-never-used"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import weekly_cap_guard as guard  # noqa: E402
from spendguard import lanes  # noqa: E402


failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def decision(payload):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        result = guard.weekly_cap_guard_pretooluse(payload)
    return result, stderr.getvalue()


task = {"tool_name": "Task", "tool_input": {"prompt": "work"}, "cwd": "/repo path"}
raw_exec = {"tool_name": "Bash", "tool_input": {"command": "codex exec 'work'"}, "cwd": "/repo"}
delegate = {"tool_name": "Bash", "tool_input": {
    "command": "spendguard delegate work --provider codex --cwd /repo"}}
original_reader = guard.anthropic_weekly_headroom
original_setting = guard._weekly_cap_guard_setting
original_disabled = guard.config.disabled
settings = {
    "enabled": True, "block_below_frac_left": 0.30, "on_unknown": "block",
    "blocked_spawn_tools": ["Task", "Agent"],
}

try:
    guard._weekly_cap_guard_setting = lambda key, default: settings.get(key, default)
    guard.config.disabled = lambda: False

    print("-- provider-truth SSOT adapter preserves percentage, reset, and unknown --")
    original_lane_headroom = lanes.lane_headroom
    lanes.lane_headroom = lambda do_fetch=False: [
        {"lane": guard.WEEKLY_CAP_GUARD_HOST_LANE, "known": True,
         "remaining_pct": 50, "reset_ts": 1792646400}]
    check("SSOT row becomes fraction-left plus unchanged reset timestamp",
          guard.anthropic_weekly_headroom() == (0.50, 1792646400))
    lanes.lane_headroom = lambda do_fetch=False: [
        {"lane": guard.WEEKLY_CAP_GUARD_HOST_LANE, "known": False,
         "remaining_pct": None, "reset_ts": None}]
    check("SSOT known=False becomes the unknown sentinel", guard.anthropic_weekly_headroom() == (None, None))
    lanes.lane_headroom = original_lane_headroom

    print("-- provider-truth headroom drives allow and deny --")
    guard.anthropic_weekly_headroom = lambda: (0.50, 1792646400)
    high, high_err = decision(task)
    check("Task at 50% is allowed", high["hookSpecificOutput"]["permissionDecision"] == "allow")
    check("readable headroom is surfaced only during spawn evaluation", "50% left" in high_err)
    guard.anthropic_weekly_headroom = lambda: (0.10, 1792646400)
    low, low_err = decision(task)
    check("Task at 10% is denied", low["hookSpecificOutput"]["permissionDecision"] == "deny")
    check("deny names threshold, exact warm-lane remedy, override, and disable",
          "10% left" in low["systemMessage"] and "30%" in low["systemMessage"]
          and "spendguard delegate --provider codex --cwd '/repo path'" in low["systemMessage"]
          and guard.WEEKLY_CAP_GUARD_OVERRIDE_ENV in low["systemMessage"]
          and guard.WEEKLY_CAP_GUARD_DISABLE_GUIDANCE in low["systemMessage"])
    check("deny JSON matches Claude Code PreToolUse schema exactly",
          set(low) == {"systemMessage", "hookSpecificOutput"}
          and low["hookSpecificOutput"] == {
              "hookEventName": "PreToolUse", "permissionDecision": "deny",
              "permissionDecisionReason": low["systemMessage"]})
    raw_low, _ = decision(raw_exec)
    check("raw codex exec at 10% is denied", raw_low["hookSpecificOutput"]["permissionDecision"] == "deny")

    print("-- non-target tools and the warm delegate route are always allowed --")
    for label, payload in (
        ("spendguard delegate", delegate), ("normal Bash", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        ("Edit", {"tool_name": "Edit", "tool_input": {}}), ("Read", {"tool_name": "Read", "tool_input": {}}),
    ):
        result, stderr = decision(payload)
        check(label + " is allowed at 10%", result["hookSpecificOutput"]["permissionDecision"] == "allow")
        check(label + " does not spam headroom", stderr == "")

    print("-- unknown policy, overrides, disable paths, and crashes --")
    guard.anthropic_weekly_headroom = lambda: (None, None)
    settings["on_unknown"] = "block"
    unknown_block, _ = decision(task)
    check("unknown + block denies Task", unknown_block["hookSpecificOutput"]["permissionDecision"] == "deny")
    settings["on_unknown"] = "warn"
    unknown_warn, _ = decision(task)
    check("unknown + warn allows Task", unknown_warn["hookSpecificOutput"]["permissionDecision"] == "allow")
    settings["on_unknown"] = "block"
    guard.anthropic_weekly_headroom = lambda: (0.10, None)
    os.environ[guard.WEEKLY_CAP_GUARD_OVERRIDE_ENV] = "1"
    overridden, override_err = decision(task)
    check("per-call override allows and warns", overridden["hookSpecificOutput"]["permissionDecision"] == "allow"
          and "override" in override_err)
    os.environ.pop(guard.WEEKLY_CAP_GUARD_OVERRIDE_ENV)
    settings["enabled"] = False
    disabled, _ = decision(task)
    check("enabled=false allows", disabled["hookSpecificOutput"]["permissionDecision"] == "allow")
    settings["enabled"] = True
    guard.config.disabled = lambda: True
    killed, _ = decision(task)
    check("GATE_DISABLE/spendguard off allows", killed["hookSpecificOutput"]["permissionDecision"] == "allow")
    guard.config.disabled = lambda: False
    guard.anthropic_weekly_headroom = lambda: (_ for _ in ()).throw(RuntimeError("reader boom"))
    crashed, crash_err = decision(task)
    check("reader crash fails open and warns", crashed["hookSpecificOutput"]["permissionDecision"] == "allow"
          and "internal error" in crash_err)

    print("-- codex exec parser recognizes invocations, not quoted mentions --")
    for command in ("codex exec work", "/opt/bin/codex exec work", "X=1 codex exec work", "echo ok && codex exec work",
                    "codex login && codex exec work"):
        check(f"positive parse: {command}", guard.command_invokes_raw_codex_exec(command))
    for command in ("spendguard delegate work --provider codex", "codex mcp-server", "codex app-server",
                    "codex login", "codex logout", "echo 'codex exec'", "printf '%s' 'codex exec'"):
        check(f"negative parse: {command}", not guard.command_invokes_raw_codex_exec(command))

    print("-- stdin/stdout entrypoint and installer contract --")
    guard.anthropic_weekly_headroom = lambda: (0.10, None)
    stdin, stdout = sys.stdin, sys.stdout
    capture = io.StringIO()
    try:
        sys.stdin, sys.stdout = io.StringIO(json.dumps(task)), capture
        exit_code = guard.cmd(["--pretooluse-hook"])
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    check("entrypoint reads JSON stdin, emits JSON stdout, and exits zero",
          exit_code == 0 and json.loads(capture.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny")

    path = pathlib.Path(_home) / "settings.json"
    sibling = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "other hook"}]}
    original = {"theme": "dark", "hooks": {"PreToolUse": [sibling], "PostToolUse": [{"hooks": [
        {"type": "command", "command": "residency-gate --posttooluse-hook"}]}]}}
    path.write_text(json.dumps(original))
    action, backup = guard.install_weekly_cap_guard(path, executable="/test/spendguard")
    installed = json.loads(path.read_text())
    check("installer preserves sibling settings and existing PostToolUse hook",
          action == "installed" and backup and installed["theme"] == "dark"
          and installed["hooks"]["PreToolUse"][0] == sibling
          and installed["hooks"]["PostToolUse"] == original["hooks"]["PostToolUse"])
    check("installer matcher lets configured spawn-tool names reach the guard",
          installed["hooks"]["PreToolUse"][-1]["matcher"] == guard.WEEKLY_CAP_GUARD_HOOK_MATCHER
          and installed["hooks"]["PreToolUse"][-1]["hooks"][0]["command"].endswith(
              guard.WEEKLY_CAP_GUARD_HOOK_SUFFIX))
    before = path.read_text()
    again, no_backup = guard.install_weekly_cap_guard(path, executable="/test/spendguard")
    check("installer is idempotent", again == "already installed" and no_backup is None and path.read_text() == before)
    removed, _ = guard.install_weekly_cap_guard(path, uninstall=True)
    check("uninstaller removes only its own hook", removed == "uninstalled" and json.loads(path.read_text()) == original)
finally:
    lanes.lane_headroom = original_lane_headroom
    guard.anthropic_weekly_headroom = original_reader
    guard._weekly_cap_guard_setting = original_setting
    guard.config.disabled = original_disabled
    os.environ.pop(guard.WEEKLY_CAP_GUARD_OVERRIDE_ENV, None)

if failures:
    raise SystemExit("FAILED: " + ", ".join(failures))
print("ok — weekly-cap guard decisions, parsing, schema, fail-open behavior, and install are pinned offline")
