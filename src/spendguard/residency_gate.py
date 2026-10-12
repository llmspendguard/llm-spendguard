"""PostToolUse residency NUDGE — the live guard for context residency (Review 1, #3b).

A tool result that lands in the MAIN conversation thread is paid on EVERY subsequent turn (cache_read re-reads it),
forever — RESIDENT context. A self-contained exploration read once in a subagent is paid ONCE — TRANSIENT. This hook
fires AT the tool call, because once a 40KB result is in the transcript nothing can remove it; it WARNS (never blocks:
the output already exists and the agent cannot un-read it — only the human can /compact or start fresh).

Two-stage, exactly as the agent-spawn gate: (1) a MECHANICAL size+turn gate (size is a MEASUREMENT, so a threshold is
legitimate) decides whether to even consider it; (2) an AGENTIC verdict — could this work's SHAPE have been delegated
(self-contained exploration) vs did it need the thread (a file about to be edited, interactive multi-step) — decided
by ONE model call on a READY $0 lane (plan_admission.ready_meta_model, so it is never refused/expensive on a capped
plan), with the verdict CONTENT-HASH cached (keyed by the evidence, not the session) in a hook-owned store so a
repeated pattern is judged once across sessions. It must NOT become the
thing it warns about: never a subagent/session spawn to make this judgement (the symgrep grep-classifier failure —
166 cold sessions, $70/day). FAIL-OPEN always: a nudge that crashes the hook would be worse than silence."""
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import sys

from . import config

_HOOK_SUFFIX = "residency-gate --posttooluse-hook"
def _residency_configured_integer(key):
    """Resolve a residency setting through the declarative config layer, including its registered default."""
    from .config_schema import SETTINGS
    spec = next(s for s in SETTINGS if s.get("section") == "residency_gate" and s.get("key") == key)
    try:
        v = os.environ.get(spec.get("env") or "")
        if v is None:
            v = config._cfg_get("residency_gate", key, spec["default"])
        return int(v)
    except (TypeError, ValueError):
        return int(spec["default"])


def _result_text(payload):
    """The tool result's text, WHOLE (its size is the measurement). Handles the dict/list/str shapes a tool_response
    takes; '' when absent."""
    r = payload.get("tool_response")
    if r is None:
        r = payload.get("tool_result")
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        c = r.get("content", r)
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return json.dumps(r, ensure_ascii=False)
    if isinstance(r, list):
        return " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in r)
    return ""


def _verdict_store_path():
    """The hook-owned delegability-verdict cache. A SINGLE owner — the PostToolUse hook path — writes it, kept apart
    from resource_state (which the main gate process owns and rewrites wholesale), so a hook subprocess can never
    clobber lane/cooldown state. Merge-written, so concurrent hook subprocesses never lose each other's verdicts."""
    return config.HOME / "residency_gate" / "verdicts.json"


def _cached_delegability_verdict(key):
    """A previously recorded {delegable, why} for this evidence hash, or None."""
    try:
        v = (json.loads(_verdict_store_path().read_text()) or {}).get(key)
        return dict(v) if isinstance(v, dict) else None
    except Exception:
        return None


def _record_delegability_verdict(key, verdict):
    """Persist a schema-checked verdict by MERGING it into the hook-owned cache (never a whole-file clobber), trimmed
    to verdict_cache_max by insertion order so the file stays bounded. Best-effort: a cache miss only re-judges on a
    $0 lane, so a write failure never fails the hook."""
    cap = max(1, _residency_configured_integer("verdict_cache_max"))

    def _merge_bounded_verdict(old):
        store = dict(old) if isinstance(old, dict) else {}
        store.pop(key, None)                        # re-insert at the end so a trim drops the OLDEST, not this one
        store[key] = dict(verdict)
        for stale in list(store)[: max(0, len(store) - cap)]:
            store.pop(stale, None)
        return store

    try:
        _verdict_store_path().parent.mkdir(parents=True, exist_ok=True)
        config.update_json(_verdict_store_path(), _merge_bounded_verdict, reason="residency-verdict-cache",
                           quarantine_unparseable=True)
    except Exception:
        pass


def _judge_residency_delegability(tool_name, tool_input, result_text):
    """Agentic delegability verdict {delegable, why}, CONTENT-HASH cached in the hook-owned verdict store. ONE call on
    a READY $0 lane (ready_meta_model — never refused on a capped plan); never a subagent/session spawn. None on any
    failure (→ the nudge stays silent, never a false alarm). Tool input and result are both supplied whole; only the
    response is bounded."""
    key = "residency:" + hashlib.sha256(json.dumps([tool_name, tool_input, result_text], sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    cached = _cached_delegability_verdict(key)
    if cached is not None:
        return cached
    try:
        from . import adapters, plan_admission, calls
        model = plan_admission.ready_meta_model(config.advisor_judge_model())
        prompt = (f"A tool result from `{tool_name}` ({len(result_text.encode('utf-8', 'ignore'))} bytes) landed in "
                  f"the MAIN conversation thread, so "
                  f"it is re-read on EVERY following turn. Could the work that produced it have been DELEGATED to a "
                  f"subagent (which reads it once and returns a short summary) — i.e. is it self-contained exploration "
                  f"— or did it need to stay in the thread (a file about to be edited, interactive multi-step work)?\n"
                  f"Tool input (whole): {json.dumps(tool_input, ensure_ascii=False)}\n"
                  f"Tool result (whole): {result_text}\n"
                  f'Reply JSON only: {{"delegable": true|false, "why": "<one short line>"}}.')
        with calls.context(intent="spendguard:residency-nudge"):
            r = adapters.call(model, prompt, sig="spendguard:residency-nudge", reasoning="minimal",
                              max_tokens=_residency_configured_integer("judge_output_tokens"),
                              schema={"type": "object", "additionalProperties": False, "required": ["delegable"],
                                      "properties": {"delegable": {"type": "boolean"}, "why": {"type": "string"}}})
        j = adapters.structured_reply(r)
    except Exception:
        return None
    if not (isinstance(j, dict) and isinstance(j.get("delegable"), bool)):
        return None
    verdict = {"delegable": bool(j["delegable"]), "why": str(j.get("why") or "")}
    _record_delegability_verdict(key, verdict)
    return verdict


def residency_nudge_thresholds_met(result_bytes, session_turns, resident_tokens):
    """Mechanical gate: result is large AND the session is long by turns OR current resident context."""
    return (result_bytes >= _residency_configured_integer("min_result_bytes")
            and (session_turns >= _residency_configured_integer("min_turns")
                 or resident_tokens >= _residency_configured_integer("min_resident_tokens")))


def evaluate_posttooluse(payload):
    """Return a PostToolUse hook dict: {} (silent) unless a LARGE result in a LONG session is judged delegable, in
    which case a systemMessage naming the resident-vs-subagent cost. Never blocks, never raises."""
    silent = {}
    try:
        tool_name = str(payload.get("tool_name") or "")
        session_id = payload.get("session_id") or "unknown-session"
        text = _result_text(payload)
        nbytes = len(text.encode("utf-8", "ignore"))
        from . import claudecode
        trajectory = claudecode.context_trajectory(session_id)
        session_turns = int(trajectory.get("turns") or 0)
        resident_tokens = int(trajectory.get("current") or 0)
        if not residency_nudge_thresholds_met(nbytes, session_turns, resident_tokens):
            return silent
        verdict = _judge_residency_delegability(tool_name, payload.get("tool_input") or {}, text)
        if not verdict or not verdict.get("delegable"):
            return silent                                  # couldn't judge, or it genuinely belongs in the thread
        toks = max(1, claudecode._toklen(text))
        rem = _residency_configured_integer("remaining_turns_est")
        model = trajectory.get("model") or config.advisor_model()
        try:
            resident, subagent = claudecode.residency_counterfactual_costs(
                model, toks, rem, _residency_configured_integer("subagent_return_tokens"))
        except Exception:
            resident = subagent = None
        cost = ""
        if resident is not None and subagent is not None:
            cost = (f" ~${resident:.2f} to re-read over ~{rem} more turns as a resident, vs ~${subagent:.4f} read once "
                    f"in a subagent")
        msg = (f"⚠ spendguard residency: a {nbytes:,}-byte {tool_name} result is now RESIDENT in the main thread"
               f"{cost}. The classifier judged it delegable: {verdict.get('why', '')}. For this SHAPE of work, prefer a "
               f"subagent (reads once, returns a summary); you can't un-read it now, but /compact or a fresh session "
               f"drops it.")
        return {"systemMessage": msg,
                "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}
    except Exception:
        return silent


def cmd(argv=None):
    args = list(argv or [])
    if "--posttooluse-hook" not in args:
        print("usage: spendguard residency-gate --posttooluse-hook", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        print(json.dumps({}))                              # bad JSON → silent (fail-open), never a crash
        return 0
    try:
        print(json.dumps(evaluate_posttooluse(payload)))
    except Exception:
        print(json.dumps({}))                              # a nudge must NEVER crash a PostToolUse hook → silent
    return 0


def _res_settings_path():
    return pathlib.Path.home() / ".claude" / "settings.json"


def _res_managed_hook(command):
    return command.endswith(_HOOK_SUFFIX)


def install_residency_gate(settings_path=None, uninstall=False, executable=None):
    """Merge or remove ONLY spendguard's PostToolUse residency nudge; back up before every changed write. Mirrors
    install_agent_gate — same careful, reversible, single-hook merge."""
    path = pathlib.Path(settings_path) if settings_path else _res_settings_path()
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except Exception as exc:
            raise RuntimeError(f"{path} is unparseable; refusing to write ({exc})") from exc
    else:
        cfg = {}
    hooks = cfg.get("hooks") or {}
    groups = list(hooks.get("PostToolUse") or [])
    found = any(_res_managed_hook(h.get("command", "")) for group in groups for h in (group.get("hooks") or []))
    if uninstall:
        revised = []
        for group in groups:
            kept = [h for h in (group.get("hooks") or []) if not _res_managed_hook(h.get("command", ""))]
            if kept:
                revised.append({**group, "hooks": kept})
        if not found:
            return "already absent", None
        if revised:
            hooks["PostToolUse"] = revised
        else:
            hooks.pop("PostToolUse", None)
        cfg["hooks"] = hooks if hooks else cfg.pop("hooks", None) or cfg.get("hooks")
        if not hooks:
            cfg.pop("hooks", None)
        action = "uninstalled"
    else:
        if found:
            return "already installed", None
        binary = executable or shutil.which("spendguard") or str(pathlib.Path(sys.prefix) / "bin" / "spendguard")
        command = f"env SPENDGUARD_NO_AUTOINSTALL=1 {shlex.quote(binary)} {_HOOK_SUFFIX}"
        groups.append({"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 30}]})
        hooks["PostToolUse"] = groups
        cfg["hooks"] = hooks
        action = "installed"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_suffix(path.suffix + ".residency-gate.bak")
    if path.exists():
        shutil.copy2(path, backup)
    config.update_json(path, lambda _old: cfg, reason="install-residency-gate", required=True)
    return action, (backup if backup.exists() else None)


def install_residency_gate_cli(argv=None):
    args = list(argv or [])
    unknown = [arg for arg in args if arg != "--uninstall"]
    if unknown:
        print("usage: spendguard install-residency-gate [--uninstall]", file=sys.stderr)
        return 2
    action, backup = install_residency_gate(uninstall="--uninstall" in args)
    print(f"{action} spendguard PostToolUse residency nudge → {_res_settings_path()}"
          + (f"  ·  backup: {backup}" if backup else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd())
