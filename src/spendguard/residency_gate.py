"""PostToolUse residency NUDGE — the live guard for context residency (Review 1, #3b).

A tool result that lands in the MAIN conversation thread is paid on EVERY subsequent turn (cache_read re-reads it),
forever — RESIDENT context. A self-contained exploration read once in a subagent is paid ONCE — TRANSIENT. This hook
fires AT the tool call, because once a 40KB result is in the transcript nothing can remove it; it WARNS (never blocks:
the output already exists and the agent cannot un-read it — only the human can /compact or start fresh).

Two-stage, exactly as the agent-spawn gate: (1) a MECHANICAL size+turn gate (size is a MEASUREMENT, so a threshold is
legitimate) decides whether to even consider it; (2) an AGENTIC verdict — could this work's SHAPE have been delegated
(self-contained exploration) vs did it need the thread (a file about to be edited, interactive multi-step) — decided
by ONE model call on a READY $0 lane (plan_admission.ready_meta_model, so it is never refused/expensive on a capped
plan), with the verdict CONTENT-HASH cached per session so a repeated pattern is judged once. It must NOT become the
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
_MIN_RESULT_BYTES_DEFAULT = 10240     # only consider a "large" result (config residency_gate.min_result_bytes)
_MIN_SESSION_TURNS_DEFAULT = 20       # only nag in a session long enough for residency to matter
_REMAINING_TURNS_EST_DEFAULT = 100    # conservative HIGH estimate of turns a resident item is re-read (backs a cost → over-estimate)
_VERDICT_OUT = 120                    # the verdict is a tiny {delegable, why} JSON — a named cap, not a magic literal


def _res_int_cfg(key, default):
    try:
        v = os.environ.get("SPENDGUARD_RESIDENCY_" + key.upper())
        if v is None:
            v = config._cfg_get("residency_gate", key, None)
        return int(v) if v is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _residency_state_path(session_id):
    digest = hashlib.sha256(str(session_id).encode()).hexdigest()
    return config.HOME / "residency_gate" / f"{digest}.json"


def _load_residency_state(session_id):
    try:
        return json.loads(_residency_state_path(session_id).read_text())
    except Exception:
        return {"verdicts": {}, "nudged": 0}


def _save_residency_state(session_id, state):
    try:
        config.update_json(_residency_state_path(session_id), lambda _old: state,
                           reason="residency-gate-session", quarantine_unparseable=True)
    except Exception:
        pass                              # state is a throttle/cache bonus; never fail the hook over it


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
        return json.dumps(r)[:200000]
    if isinstance(r, list):
        return " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in r)
    return ""


def _session_turns(session_id):
    """The session's billed turn count from the ledger (claude-code est-value rows), or 0 when unknown. $0."""
    try:
        from . import claudecode
        return int(claudecode.context_trajectory(session_id).get("turns") or 0)
    except Exception:
        return 0


def _rates():
    """(cache_read $/tok, metered_input $/tok) for the advisor judge model's family — from pricing only, (None, None)
    when unpriced so a cost is never invented."""
    try:
        from . import claudecode, pricing
        model = config.advisor_model()
        cr = claudecode._cache_read_rate(model)
        p = pricing.price(model) or {}
        inr = (float(p.get("in_")) / 1e6) if p.get("in_") is not None else None
        return cr, inr
    except Exception:
        return None, None


def _residency_verdict(session_id, tool_name, tool_input, result_bytes, head):
    """Agentic delegability verdict {delegable, why}, CONTENT-HASH cached per session. ONE call on a READY $0 lane
    (ready_meta_model — never refused on a capped plan); never a subagent/session spawn. None on any failure (→ the
    nudge stays silent, never a false alarm). The evidence is the work's SHAPE (tool + input + size + a head preview),
    which is small and passed whole — the full result bytes are NOT the evidence for a shape judgement."""
    key = hashlib.sha256(json.dumps([tool_name, tool_input, result_bytes], sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    state = _load_residency_state(session_id)
    cached = (state.get("verdicts") or {}).get(key)
    if cached is not None:
        return cached, key, state
    try:
        from . import adapters, plan_admission, calls
        model = plan_admission.ready_meta_model(config.advisor_judge_model())
        # SHAPE evidence passed WHOLE: every tool_input key is preserved; only a bulky string VALUE becomes a
        # '<N chars>' size marker — bounded by CONTAINMENT, never a byte-cut that could drop a shape-relevant field
        # (a path / command / pattern). A Write's content body is size, not shape, so a marker loses no decision evidence.
        shape = ({k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 400 else v)
                  for k, v in tool_input.items()} if isinstance(tool_input, dict) else tool_input)
        prompt = (f"A tool result from `{tool_name}` ({result_bytes} bytes) landed in the MAIN conversation thread, so "
                  f"it is re-read on EVERY following turn. Could the work that produced it have been DELEGATED to a "
                  f"subagent (which reads it once and returns a short summary) — i.e. is it self-contained exploration "
                  f"— or did it need to stay in the thread (a file about to be edited, interactive multi-step work)?\n"
                  f"Tool: {tool_name}  ·  input (shape, whole): {json.dumps(shape)}  ·  result preview: {head}\n"
                  f'Reply JSON only: {{"delegable": true|false, "why": "<one short line>"}}.')
        with calls.context(intent="spendguard:residency-nudge"):
            r = adapters.call(model, prompt, sig="spendguard:residency-nudge", max_tokens=_VERDICT_OUT,
                              schema={"type": "object", "additionalProperties": False, "required": ["delegable"],
                                      "properties": {"delegable": {"type": "boolean"}, "why": {"type": "string"}}})
        j = adapters.structured_reply(r)
    except Exception:
        return None, key, state                           # degrade to silence — a nudge must never crash the hook (incl. a spend refusal)
    if not (isinstance(j, dict) and isinstance(j.get("delegable"), bool)):
        return None, key, state
    verdict = {"delegable": bool(j["delegable"]), "why": (j.get("why") or "")[:160]}
    state.setdefault("verdicts", {})[key] = verdict
    _save_residency_state(session_id, state)
    return verdict, key, state


def evaluate_posttooluse(payload):
    """Return a PostToolUse hook dict: {} (silent) unless a LARGE result in a LONG session is judged delegable, in
    which case a systemMessage naming the resident-vs-subagent cost. Never blocks, never raises."""
    silent = {}
    try:
        tool_name = str(payload.get("tool_name") or "")
        session_id = payload.get("session_id") or "unknown-session"
        text = _result_text(payload)
        nbytes = len(text.encode("utf-8", "ignore"))
        if nbytes < _res_int_cfg("min_result_bytes", _MIN_RESULT_BYTES_DEFAULT):
            return silent                                  # mechanical size gate — too small to matter
        if _session_turns(session_id) < _res_int_cfg("min_turns", _MIN_SESSION_TURNS_DEFAULT):
            return silent                                  # too early in the session for residency to bite
        verdict, _key, _state = _residency_verdict(session_id, tool_name, payload.get("tool_input") or {}, nbytes, text[:500])
        if not verdict or not verdict.get("delegable"):
            return silent                                  # couldn't judge, or it genuinely belongs in the thread
        toks = max(1, nbytes // 4)
        rem = _res_int_cfg("remaining_turns_est", _REMAINING_TURNS_EST_DEFAULT)
        cr_rate, in_rate = _rates()
        resident = (toks * rem * cr_rate) if cr_rate is not None else None
        subagent = (toks * in_rate) if in_rate is not None else None             # read ONCE, returns a short summary
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
