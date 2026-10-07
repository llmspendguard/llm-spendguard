"""PreToolUse gate that routes avoidable Agent/Task work off a near-cap hosting plan."""
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import sys

from . import config, delegate_router, lane_registry, lanes, overage

_HOOK_SUFFIX = "agent-spawn-gate --pretooluse-hook"
_OVERRIDE_FLAG = "SPENDGUARD_ALLOW_SUBAGENT"
_OVERRIDE_REASON = "SPENDGUARD_ALLOW_SUBAGENT_REASON"


def _hosting_lane():
    configured = config._cfg_get("agent_spawn_gate", "plan_lane", None)
    if configured:
        return configured
    marked = [row["lane"] for row in lane_registry.LANES if row.get("subagent_host")]
    if len(marked) != 1:
        raise RuntimeError("lane_registry must mark exactly one subagent_host, or configure agent_spawn_gate.plan_lane")
    return marked[0]


def _plan_in_band():
    if overage.current_overage_status().get("on_overage_now"):
        return True
    from . import config_schema
    declared = next(item for item in config_schema.SETTINGS
                    if item["section"] == "agent_spawn_gate" and item["key"] == "near_cap_remaining_pct")
    threshold = float(config._cfg_get("agent_spawn_gate", "near_cap_remaining_pct", declared["default"]))
    row = next((item for item in lanes.lane_headroom(do_fetch=False)
                if item.get("lane") == _hosting_lane()), None)
    return bool(row and row.get("known") and row.get("remaining_pct") is not None
                and float(row["remaining_pct"]) <= threshold)


def _agent_spawn_state_path(session_id):
    digest = hashlib.sha256(str(session_id).encode()).hexdigest()
    return config.HOME / "agent_spawn_gate" / f"{digest}.json"


def _load_session_state(session_id):
    try:
        return json.loads(_agent_spawn_state_path(session_id).read_text())
    except Exception:
        return {"verdicts": {}, "last_kind": None, "denials": 0, "in_band": None}


def _save_session_state(session_id, state):
    config.update_json(_agent_spawn_state_path(session_id), lambda _old: state,
                       reason="agent-spawn-gate-session", quarantine_unparseable=True)


def _hook_result(decision, message):
    return {"systemMessage": message, "hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision,
        "permissionDecisionReason": message}}


def _spawn_fields(payload):
    tool_input = payload.get("tool_input") or {}
    return str(tool_input.get("prompt") or ""), str(tool_input.get("subagent_type") or "")


def _verdict_cache_key(prompt, subagent_type):
    whole = json.dumps([prompt, subagent_type], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(whole.encode()).hexdigest()


def _deny_message(verdict, prompt, full):
    why = verdict.get("why") or "the classifier found an off-plan route"
    if not full:
        return f"spendguard denied repeated {verdict['kind']} subagent; use the route emitted on the first denial."
    if verdict["kind"] == "oneshot":
        command = f"spendguard ask {shlex.quote(prompt)}"
    else:
        base = f"spendguard delegate {shlex.quote(prompt)}"
        command = f"{base} --estimate`, then `{base} --yes"
    provider = verdict.get("provider") or "configured off-plan lane"
    return (f"spendguard denied this subagent while the hosting plan is near cap/overage. "
            f"Classifier: {verdict['kind']} via {provider} — {why}. Run `{command}`.")


def evaluate_pretooluse(payload):
    """Return Claude Code PreToolUse JSON for one Agent/Task spawn payload."""
    prompt, subagent_type = _spawn_fields(payload)
    session_id = payload.get("session_id") or "unknown-session"
    override = os.getenv(_OVERRIDE_FLAG) == "1"
    reason = (os.getenv(_OVERRIDE_REASON) or "").strip()
    if override and reason:
        return _hook_result("allow", f"spendguard subagent override recorded: {reason}")
    if override:
        return _hook_result("deny", f"{_OVERRIDE_FLAG}=1 requires {_OVERRIDE_REASON} with a human-supplied reason")
    try:
        in_band = _plan_in_band()
    except Exception as exc:
        return _hook_result("allow", f"spendguard agent gate degraded; allowing spawn: plan state unavailable ({exc})")
    if not in_band:
        state = _load_session_state(session_id)
        state["in_band"] = False
        _save_session_state(session_id, state)
        return _hook_result("allow", "spendguard agent gate: hosting plan has headroom")

    state = _load_session_state(session_id)
    key = _verdict_cache_key(prompt, subagent_type)
    verdict = (state.get("verdicts") or {}).get(key)
    if verdict is None:
        try:
            verdict = delegate_router.classify_task(prompt, exclude_plans=[_hosting_lane()])
        except Exception as exc:
            return _hook_result("allow", f"spendguard agent gate degraded; allowing spawn: classifier error ({exc})")
        if verdict.get("error"):
            return _hook_result("allow", "spendguard agent gate degraded; allowing spawn: " + verdict["error"])
        state.setdefault("verdicts", {})[key] = verdict

    kind = verdict.get("kind")
    denied = kind == "oneshot" or (kind == "agentic" and verdict.get("self_contained") is True)
    changed = state.get("last_kind") != kind or state.get("in_band") is not True
    state["last_kind"] = kind
    state["in_band"] = True
    if denied:
        full = not state.get("denials") or changed
        state["denials"] = int(state.get("denials") or 0) + 1
        message = _deny_message(verdict, prompt, full)
        _save_session_state(session_id, state)
        return _hook_result("deny", message)
    _save_session_state(session_id, state)
    return _hook_result("allow", f"spendguard agent gate allowed {kind}: {verdict.get('why', '')}")


def cmd(argv=None):
    args = list(argv or [])
    if "--pretooluse-hook" not in args:
        print("usage: spendguard agent-spawn-gate --pretooluse-hook", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps(_hook_result("allow", f"spendguard agent gate degraded; allowing spawn: invalid hook JSON ({exc})")))
        return 0
    try:
        result = evaluate_pretooluse(payload)
    except Exception as exc:                 # A gate on EVERY Agent/Task spawn must NEVER crash: a crashed PreToolUse
        # hook can fail-CLOSED and block all agent work. So any unanticipated error (beyond the per-step try/excepts
        # inside evaluate_pretooluse) degrades to ALLOW, visibly — the fail-open guarantee is the outermost invariant.
        result = _hook_result("allow", "spendguard agent gate degraded; allowing spawn: unexpected error "
                              f"({type(exc).__name__}: {str(exc)[:160]})")
    print(json.dumps(result))
    return 0


def _settings_path():
    return pathlib.Path.home() / ".claude" / "settings.json"


def _managed_hook(command):
    return command.endswith(_HOOK_SUFFIX)


def install_agent_gate(settings_path=None, uninstall=False, executable=None):
    """Merge or remove only spendguard's Agent|Task PreToolUse hook; back up before every changed write."""
    path = pathlib.Path(settings_path) if settings_path else _settings_path()
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except Exception as exc:
            raise RuntimeError(f"{path} is unparseable; refusing to write ({exc})") from exc
    else:
        cfg = {}
    hooks = cfg.get("hooks") or {}
    groups = list(hooks.get("PreToolUse") or [])
    found = any(_managed_hook(h.get("command", "")) for group in groups for h in (group.get("hooks") or []))
    if uninstall:
        revised = []
        for group in groups:
            kept = [h for h in (group.get("hooks") or []) if not _managed_hook(h.get("command", ""))]
            if kept:
                revised.append({**group, "hooks": kept})
        if not found:
            return "already absent", None
        if revised:
            hooks["PreToolUse"] = revised
        else:
            hooks.pop("PreToolUse", None)
        if hooks:
            cfg["hooks"] = hooks
        else:
            cfg.pop("hooks", None)
        action = "uninstalled"
    else:
        if found:
            return "already installed", None
        binary = executable or shutil.which("spendguard") or str(pathlib.Path(sys.prefix) / "bin" / "spendguard")
        command = f"env SPENDGUARD_NO_AUTOINSTALL=1 {shlex.quote(binary)} {_HOOK_SUFFIX}"
        groups.append({"matcher": "Agent|Task", "hooks": [
            {"type": "command", "command": command, "timeout": 30}]})
        hooks["PreToolUse"] = groups
        cfg["hooks"] = hooks
        action = "installed"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".agent-gate.bak")
    if path.exists():
        shutil.copy2(path, backup)
    config.update_json(path, lambda _old: cfg, reason="install-agent-gate", required=True)
    return action, (backup if backup.exists() else None)


def install_agent_gate_cli(argv=None):
    args = list(argv or [])
    unknown = [arg for arg in args if arg != "--uninstall"]
    if unknown:
        print("usage: spendguard install-agent-gate [--uninstall]", file=sys.stderr)
        return 2
    action, backup = install_agent_gate(uninstall="--uninstall" in args)
    print(f"{action} spendguard Agent/Task gate → {_settings_path()}"
          + (f"  ·  backup: {backup}" if backup else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd())
