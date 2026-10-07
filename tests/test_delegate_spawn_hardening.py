"""Offline guards for delegated provider stdin and working-directory containment."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-delegate-spawn-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-delegate-spawn-never-used")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import antigravity_exec, codex_daemon, codex_exec, delegate_router, kimi_exec, subscription_exec  # noqa: E402

failed = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failed.append(label)


def exercise_cold(module, stdout, poison_cwd, output_file=False):
    seen = {}
    original_bin, original_run = module._bin, module.subprocess.run
    original_daemon = getattr(module, "_daemon_enabled", None)
    original_getcwd = os.getcwd
    try:
        os.getcwd = lambda: poison_cwd
        module._bin = lambda: "/fake/provider"
        if original_daemon is not None:
            module._daemon_enabled = lambda: False

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            if output_file:
                pathlib.Path(cmd[cmd.index("--output-last-message") + 1]).write_text("answer")
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        module.subprocess.run = fake_run
        result = module.run_prompt("offline prompt")
    finally:
        os.getcwd = original_getcwd
        module._bin, module.subprocess.run = original_bin, original_run
        if original_daemon is not None:
            module._daemon_enabled = original_daemon
    return result, seen


print("-- every cold provider run disconnects stdin and uses a removed neutral cwd --")
poison = os.path.join(tempfile.gettempdir(), "SPENDGUARD-AMBIENT-CWD-POISON")
cases = [
    (codex_exec, "", True),
    (antigravity_exec, json.dumps({"status": "SUCCESS", "response": "answer", "usage": {}}), False),
    (subscription_exec, json.dumps({"result": "answer", "usage": {"input_tokens": 1, "output_tokens": 1}}), False),
    (kimi_exec, json.dumps({"role": "assistant", "content": "answer"}), False),
]
for module, stdout, output_file in cases:
    result, seen = exercise_cold(module, stdout, poison, output_file)
    name = module.__name__.rsplit(".", 1)[-1]
    check(f"{name} returns through its offline spawn", not result.get("error"))
    check(f"{name} passes DEVNULL stdin", seen.get("stdin") is subprocess.DEVNULL)
    selected_cwd = seen.get("cwd")
    check(f"{name} never uses ambient cwd", selected_cwd != poison)
    check(f"{name} uses a removed neutral cwd", bool(selected_cwd) and not os.path.exists(selected_cwd))

print("-- ambient cwd is poison, explicit workspace is required and preserved --")
original_getcwd = os.getcwd
original_warm = codex_daemon.run_warm
original_enabled = codex_exec._daemon_enabled
warm_seen = {}
try:
    os.getcwd = lambda: poison
    codex_exec._daemon_enabled = lambda: True

    def fake_warm(_prompt, **kwargs):
        warm_seen.update(kwargs)
        return {"text": "warm answer", "error": None}

    codex_daemon.run_warm = fake_warm
    read_only = codex_exec.run_prompt("read")
finally:
    os.getcwd = original_getcwd
    codex_daemon.run_warm = original_warm
    codex_exec._daemon_enabled = original_enabled
check("warm read-only run succeeds", read_only.get("text") == "warm answer")
check("warm run never receives ambient cwd", warm_seen.get("cwd") != poison)
check("warm read-only neutral cwd is removed", not os.path.exists(warm_seen["cwd"]))

refused = codex_exec.run_prompt("edit", sandbox="workspace-write")
check("spawn layer refuses workspace-write without cwd", "explicit trusted cwd" in refused.get("error", ""))

classification = {"kind": "agentic", "provider": "codex", "self_contained": True, "why": "offline"}
original_classify = delegate_router.classify_task
original_ready = delegate_router.delegation_lanes_ready
original_estimate = delegate_router._estimate_route
try:
    delegate_router.classify_task = lambda *args, **kwargs: dict(classification)
    delegate_router.delegation_lanes_ready = lambda _kind: {"ready": ["codex"]}
    delegate_router._estimate_route = lambda *args, **kwargs: {
        "plan": "codex", "model": "test-model", "real_api_usd": 0.0, "est_value_usd": 0.0,
        "input_tokens": 1, "output_tokens": 1,
    }
    delegated = delegate_router.delegate_task("edit", execute=True)
finally:
    delegate_router.classify_task = original_classify
    delegate_router.delegation_lanes_ready = original_ready
    delegate_router._estimate_route = original_estimate
check("router returns a structured refusal without cwd", delegated.get("status") == "refused")
check("router refusal tells caller to pass target repo", "target repo path" in delegated.get("why", ""))

check("daemon fallback cwd is fixed safe cwd, not ambient cwd",
      os.path.abspath(codex_daemon._SAFE_CWD) != poison)

if failed:
    raise SystemExit(f"FAILED: {', '.join(failed)}")
print("ok — delegated spawns disconnect stdin and never inherit the server cwd")
