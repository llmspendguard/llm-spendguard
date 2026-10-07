"""Offline contract for the Agent/Task PreToolUse gate and settings merger."""
import json
import io
import os
import pathlib
import sys
import tempfile

_home = tempfile.mkdtemp(prefix="sg-agent-spawn-gate-")
os.environ["SPENDGUARD_HOME"] = _home
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "sk-test-agent-spawn-gate-never-used"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import agent_spawn_gate as gate  # noqa: E402

_failed = []


def check(label, condition):
    ok = bool(condition)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    if not ok:
        _failed.append(label)


classify_calls = []
classifier_kwargs = []
current = {"kind": "oneshot", "provider": "test-plan", "self_contained": True, "why": "independent"}


def fake_classify(prompt, **kwargs):
    classify_calls.append(prompt)
    classifier_kwargs.append(kwargs)
    return dict(current)


original_classify = gate.delegate_router.classify_task
original_band = gate._plan_in_band
gate.delegate_router.classify_task = fake_classify

try:
    prompt = "FIRST\n" + ("all evidence must survive\n" * 2000) + "LAST-SENTINEL"
    payload = {"session_id": "outside", "tool_name": "Agent",
               "tool_input": {"prompt": prompt, "subagent_type": "Explore"}}

    print("-- plan-state short circuit and complete classifier evidence --")
    gate._plan_in_band = lambda: False
    result = gate.evaluate_pretooluse(payload)
    check("out-of-band allows", result["hookSpecificOutput"]["permissionDecision"] == "allow")
    check("out-of-band makes ZERO classify calls", classify_calls == [])
    gate._plan_in_band = lambda: True
    result = gate.evaluate_pretooluse(payload)
    check("whole spawn prompt reaches classifier", classify_calls == [prompt] and classify_calls[0].endswith("LAST-SENTINEL"))
    check("classifier excludes the hosting plan without changing the prompt",
          classifier_kwargs == [{"exclude_plans": [gate._hosting_lane()]}])
    check("oneshot denies with concrete command", result["hookSpecificOutput"]["permissionDecision"] == "deny"
          and "spendguard ask" in result["systemMessage"])
    before = len(classify_calls)
    repeated = gate.evaluate_pretooluse(payload)
    check("identical session spawn uses verdict cache", len(classify_calls) == before)
    check("repeat denial is rate-limited to one line",
          repeated["systemMessage"].count("\n") == 0 and len(repeated["systemMessage"]) < len(result["systemMessage"]))

    print("-- every verdict maps to its specified action --")
    current.update(kind="agentic", self_contained=True, why="isolated edit")
    agentic = gate.evaluate_pretooluse({"session_id": "agentic", "tool_input": {
        "prompt": "edit and test", "subagent_type": "general-purpose"}})
    check("self-contained agentic denies with estimate and yes commands",
          agentic["hookSpecificOutput"]["permissionDecision"] == "deny"
          and "--estimate" in agentic["systemMessage"] and "--yes" in agentic["systemMessage"])
    current.update(kind="agentic", self_contained=False, why="needs live state")
    nonself = gate.evaluate_pretooluse({"session_id": "nonself", "tool_input": {
        "prompt": "continue current live work", "subagent_type": "general-purpose"}})
    check("non-self-contained agentic allows", nonself["hookSpecificOutput"]["permissionDecision"] == "allow")
    current.update(kind="needs_claude", self_contained=False, provider=None, why="depends on conversation")
    needs = gate.evaluate_pretooluse({"session_id": "needs", "tool_input": {
        "prompt": "use the context above", "subagent_type": "general-purpose"}})
    check("needs_claude allows", needs["hookSpecificOutput"]["permissionDecision"] == "allow")

    print("-- degradation and explicit override stay visible --")
    gate.delegate_router.classify_task = lambda _prompt, **_kwargs: {"error": "test lane unreachable"}
    degraded = gate.evaluate_pretooluse({"session_id": "degraded", "tool_input": {
        "prompt": "classify me", "subagent_type": "Explore"}})
    check("classifier degradation allows", degraded["hookSpecificOutput"]["permissionDecision"] == "allow")
    check("classifier degradation emits visible reason", "degraded" in degraded["systemMessage"]
          and "test lane unreachable" in degraded["systemMessage"])
    os.environ["SPENDGUARD_ALLOW_SUBAGENT"] = "1"
    os.environ["SPENDGUARD_ALLOW_SUBAGENT_REASON"] = "human approved urgent live-context work"
    override = gate.evaluate_pretooluse(payload)
    check("escape hatch allows and records human reason", override["hookSpecificOutput"]["permissionDecision"] == "allow"
          and "human approved urgent live-context work" in override["systemMessage"])
    del os.environ["SPENDGUARD_ALLOW_SUBAGENT"]
    del os.environ["SPENDGUARD_ALLOW_SUBAGENT_REASON"]

    print("-- stdin/stdout hook entrypoint mirrors the receipt hook convention --")
    gate.delegate_router.classify_task = fake_classify
    current.update(kind="needs_claude", self_contained=False, provider=None, why="entrypoint check")
    original_stdin, original_stdout = sys.stdin, sys.stdout
    capture = io.StringIO()
    try:
        sys.stdin = io.StringIO(json.dumps({"session_id": "cmd", "tool_input": {
            "prompt": "complete prompt through stdin", "subagent_type": "general-purpose"}}))
        sys.stdout = capture
        exit_code = gate.cmd(["--pretooluse-hook"])
    finally:
        sys.stdin, sys.stdout = original_stdin, original_stdout
    entrypoint = json.loads(capture.getvalue())
    check("hook entrypoint reads stdin, emits decision JSON, and exits zero",
          exit_code == 0 and entrypoint["hookSpecificOutput"]["permissionDecision"] == "allow")

    print("-- the hook NEVER crashes: an unexpected internal error still emits ALLOW (fail-open) --")
    _orig_eval = gate.evaluate_pretooluse

    def _boom(_payload):
        raise RuntimeError("unexpected internal failure")

    gate.evaluate_pretooluse = _boom
    original_stdin, original_stdout = sys.stdin, sys.stdout
    capture = io.StringIO()
    try:
        sys.stdin = io.StringIO(json.dumps({"session_id": "boom",
                                            "tool_input": {"prompt": "x", "subagent_type": "Explore"}}))
        sys.stdout = capture
        boom_code = gate.cmd(["--pretooluse-hook"])
    finally:
        sys.stdin, sys.stdout = original_stdin, original_stdout
        gate.evaluate_pretooluse = _orig_eval
    boom_out = json.loads(capture.getvalue())
    check("unexpected internal error degrades to ALLOW and exits zero (never fail-closed)",
          boom_code == 0 and boom_out["hookSpecificOutput"]["permissionDecision"] == "allow"
          and "unexpected error" in boom_out["systemMessage"])

    print("-- installer is preserving, idempotent, backed up, and reversible --")
    settings = pathlib.Path(_home) / "settings.json"
    sibling = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "other-tool hook"}]}
    original = {"theme": "dark", "hooks": {"PreToolUse": [sibling], "Stop": [{"hooks": [
        {"type": "command", "command": "receipt-tool"}]}]}}
    settings.write_text(json.dumps(original))
    action, backup = gate.install_agent_gate(settings, executable="/test/spendguard")
    installed = json.loads(settings.read_text())
    check("installer reports installed and backs up first", action == "installed" and backup and backup.exists()
          and json.loads(backup.read_text()) == original)
    check("installer clobbers no sibling hook or key", installed["theme"] == "dark"
          and installed["hooks"]["PreToolUse"][0] == sibling and installed["hooks"]["Stop"] == original["hooks"]["Stop"])
    before = settings.read_text()
    action2, backup2 = gate.install_agent_gate(settings, executable="/test/spendguard")
    check("installer is idempotent", action2 == "already installed" and backup2 is None and settings.read_text() == before)
    removed, _ = gate.install_agent_gate(settings, uninstall=True)
    check("uninstall removes exactly the managed entry", removed == "uninstalled"
          and json.loads(settings.read_text()) == original)
    settings.write_text("{broken")
    try:
        gate.install_agent_gate(settings, executable="/test/spendguard")
        parse_failed = False
    except RuntimeError:
        parse_failed = True
    check("unparseable settings fail loudly without rewrite", parse_failed and settings.read_text() == "{broken")
finally:
    gate.delegate_router.classify_task = original_classify
    gate._plan_in_band = original_band

if _failed:
    raise SystemExit(f"FAILED: {', '.join(_failed)}")
print("ok — Agent/Task spawn gate routes, degrades visibly, caches, and preserves settings offline")
