"""D15: registry capability must dispatch a tool-enabled CLI in the supplied workspace."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spendguard import config, delegate_router, lane_registry

CROSSPROVIDER_AGENTIC_CASES = (
    ("gemini", "antigravity_exec", ("--mode", "auto")),
    ("kimi-code", "kimi_exec", ("--auto",)),
)
CROSSPROVIDER_OFFLINE_MODEL = "offline-model"
CROSSPROVIDER_OFFLINE_TASK = "Perform the supplied workspace task"
CROSSPROVIDER_EDIT_FILENAME = "delegate-output"
CROSSPROVIDER_OFFLINE_EXECUTABLE = "offline-agent"
CROSSPROVIDER_OFFLINE_RESULT = "completed"
CROSSPROVIDER_CREDENTIAL_SUFFIX = "-credential"


@pytest.mark.parametrize("plan,module_name,agent_flags", CROSSPROVIDER_AGENTIC_CASES)
def test_crossprovider_agentic_dispatch_edits_supplied_workspace(plan, module_name, agent_flags,
                                                               tmp_path, monkeypatch):
    module = importlib.import_module("spendguard." + module_name)
    workspace = tmp_path / plan
    workspace.mkdir()
    dispatched_commands = []

    def crossprovider_offline_transport(command, **kwargs):
        dispatched_commands.append(command)
        assert kwargs["cwd"] == str(workspace)
        assert all(flag in command for flag in agent_flags)
        (Path(kwargs["cwd"]) / CROSSPROVIDER_EDIT_FILENAME).write_text(plan)
        payload = ({"status": "SUCCESS", "response": CROSSPROVIDER_OFFLINE_RESULT}
                   if module_name == "antigravity_exec" else
                   {"role": "assistant", "content": CROSSPROVIDER_OFFLINE_RESULT})
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(module, "_bin", lambda: CROSSPROVIDER_OFFLINE_EXECUTABLE)
    monkeypatch.setattr(module.subprocess, "run", crossprovider_offline_transport)
    monkeypatch.setattr(config, "lane_exec_env", lambda unused: {})
    monkeypatch.setattr(delegate_router, "_invoke_recoverable_delegate", lambda executor, **kwargs: executor())
    from spendguard import delegate_worktrees
    monkeypatch.setattr(delegate_worktrees, "run_isolated_delegate", lambda executor, cwd, **isolation_dependencies: executor(cwd))
    if module_name == "kimi_exec":
        monkeypatch.setattr(module, "_daemon_enabled", lambda: True)
        monkeypatch.setattr(module, "_warm_model_eligible", lambda unused: True)
        from spendguard import kimi_daemon
        monkeypatch.setattr(kimi_daemon, "run_warm", lambda *args, **kwargs: pytest.fail(
            "agentic work reached the neutral read-only warm daemon"))
    assert lane_registry.lane_spec(plan)["agentic"] is True
    execution = delegate_router._route_agentic(plan, CROSSPROVIDER_OFFLINE_TASK,
                                               CROSSPROVIDER_OFFLINE_MODEL, None, str(workspace))
    assert not execution.get("error"), execution
    assert dispatched_commands
    assert (workspace / CROSSPROVIDER_EDIT_FILENAME).read_text() == plan


def test_crossprovider_http_lane_never_advertised_or_dispatched_agentic(tmp_path, monkeypatch):
    from spendguard import zai_exec
    plan = lane_registry.provider_lane_map()["zai"][0]
    monkeypatch.setattr(delegate_router, "_configured_plan_models", lambda: {plan: CROSSPROVIDER_OFFLINE_MODEL})
    monkeypatch.setattr(zai_exec, "available", lambda: True)
    monkeypatch.setattr(zai_exec, "run_prompt", lambda *args, **kwargs: pytest.fail(
        "agentic work reached a completion-only HTTP executor"))
    assert lane_registry.lane_spec(plan)["agentic"] is False
    assert plan not in delegate_router.delegation_lanes_ready("agentic")["ready"]
    assert delegate_router._route_agentic(plan, CROSSPROVIDER_OFFLINE_TASK,
                                         CROSSPROVIDER_OFFLINE_MODEL, None, str(tmp_path))["error"]


@pytest.mark.parametrize("plan,module_name,agent_flags", CROSSPROVIDER_AGENTIC_CASES)
def test_crossprovider_agentic_readiness_uses_cli_credentials(plan, module_name, agent_flags,
                                                           tmp_path, monkeypatch):
    module = importlib.import_module("spendguard." + module_name)
    credential = tmp_path / (plan + CROSSPROVIDER_CREDENTIAL_SUFFIX)
    credential.touch()
    monkeypatch.setitem(lane_registry.lane_spec(plan), "creds", (credential,))
    monkeypatch.setattr(module, "available", lambda: True)
    monkeypatch.setattr(delegate_router, "_configured_plan_models", lambda: {plan: CROSSPROVIDER_OFFLINE_MODEL})
    assert delegate_router.delegation_lanes_ready("agentic")["ready"] == [plan]
    credential.unlink()
    assert delegate_router.delegation_lanes_ready("agentic")["ready"] == []


@pytest.mark.parametrize("plan,module_name,agent_flags", CROSSPROVIDER_AGENTIC_CASES)
def test_crossprovider_agentic_missing_workspace_fails_closed(plan, module_name, agent_flags, monkeypatch):
    module = importlib.import_module("spendguard." + module_name)
    monkeypatch.setattr(module, "_bin", lambda: CROSSPROVIDER_OFFLINE_EXECUTABLE)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail(
        "agentic execution without an explicit workspace must not spawn a child"))
    assert module.run_prompt(CROSSPROVIDER_OFFLINE_TASK, agentic=True)["error"]


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as crossprovider_test_root:
        for crossprovider_case in CROSSPROVIDER_AGENTIC_CASES:
            with pytest.MonkeyPatch.context() as crossprovider_patch:
                test_crossprovider_agentic_dispatch_edits_supplied_workspace(
                    *crossprovider_case, Path(crossprovider_test_root), crossprovider_patch)
            with pytest.MonkeyPatch.context() as crossprovider_patch:
                test_crossprovider_agentic_readiness_uses_cli_credentials(
                    *crossprovider_case, Path(crossprovider_test_root), crossprovider_patch)
            with pytest.MonkeyPatch.context() as crossprovider_patch:
                test_crossprovider_agentic_missing_workspace_fails_closed(*crossprovider_case, crossprovider_patch)
        with pytest.MonkeyPatch.context() as crossprovider_patch:
            test_crossprovider_http_lane_never_advertised_or_dispatched_agentic(
                Path(crossprovider_test_root), crossprovider_patch)
    print("cross-provider agentic dispatch: 7 targeting cases passed")
