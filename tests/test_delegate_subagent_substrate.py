"""Offline guards for the trustworthy delegated-subagent substrate."""
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from spendguard import codex_exec, delegate_router

SUBSTRATE_TEST_PLAN = "codex"
SUBSTRATE_TEST_MODEL = "fixture-model"
SUBSTRATE_TEST_PROVIDER = "openai"
SUBSTRATE_TEST_TASK = "labelled routing fixture"
SUBSTRATE_TEST_TOOL = "configured-code-intelligence"
SUBSTRATE_CHANGED_FILE = "delegate-fixture.py"
SUBSTRATE_NON_COMPLETION_RESULT = "I could not complete the requested task."
SUBSTRATE_TEST_TIMEOUT_SECONDS = 7
SUBSTRATE_LABELLED_ROUTE_FIXTURES = (
    ("Edit the supplied repository and run its tests without asking follow-up questions.",
     delegate_router.DELEGATE_ROUTE_CODEX),
    ("Operate the host's currently open Claude desktop pane using its live application state.",
     delegate_router.DELEGATE_ROUTE_CLAUDE_SUBAGENT),
    ("Independently summarize every supplied document with one result per document.",
     delegate_router.DELEGATE_ROUTE_LANE),
    ("Help me choose the next design interactively after each answer.",
     delegate_router.DELEGATE_ROUTE_MAIN_THREAD),
    ("From the supplied repository, invoke the configured MCP code-intelligence and review tools and report evidence.",
     delegate_router.DELEGATE_ROUTE_CODEX),
)


def test_delegate_route_classifier_uses_model_for_every_label():
    observed_prompts = []
    correct_routes = 0
    for labelled_task, labelled_route in SUBSTRATE_LABELLED_ROUTE_FIXTURES:
        remote_route = labelled_route in (
            delegate_router.DELEGATE_ROUTE_CODEX, delegate_router.DELEGATE_ROUTE_LANE)
        model_reply = {"route": labelled_route,
                       "provider": SUBSTRATE_TEST_PLAN if remote_route else None,
                       "self_contained": remote_route, "why": "labelled fixture reason"}

        def substrate_classifier_reply(prompt, model_reply=model_reply, **kwargs):
            observed_prompts.append(prompt)
            return {"text": json.dumps(model_reply), "lane": SUBSTRATE_TEST_PLAN,
                    "model": SUBSTRATE_TEST_MODEL}

        with patch.object(delegate_router, "_configured_plan_models",
                          return_value={SUBSTRATE_TEST_PLAN: SUBSTRATE_TEST_MODEL}), \
                patch.object(delegate_router.config, "read_json_cache_entry", return_value=None), \
                patch.object(delegate_router, "_write_cached_delegate_verdict"), \
                patch.object(delegate_router.lane_balance, "delegate",
                             side_effect=substrate_classifier_reply), \
                patch("spendguard.gate.require"):
            verdict = delegate_router.classify_task(labelled_task)
        correct_routes += verdict["route"] == labelled_route
        assert verdict["decision_path"] == "model"
    labelled_fixture_count = len(SUBSTRATE_LABELLED_ROUTE_FIXTURES)
    measured_accuracy = correct_routes / labelled_fixture_count
    assert measured_accuracy == 1.0, f"classifier labelled-route accuracy={measured_accuracy:.1%}"
    assert len(observed_prompts) == labelled_fixture_count


def test_delegate_classifier_cache_is_content_addressed():
    first = delegate_router.delegate_route_fingerprint(SUBSTRATE_TEST_TASK)
    second = delegate_router.delegate_route_fingerprint(SUBSTRATE_TEST_TASK + " changed")
    assert first != second


def test_codex_delegate_enables_mcp_and_scoped_automatic_review(tmp_path):
    output_text = "tool-backed result"

    def substrate_completed_process(command, timeout, environment, cwd):
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(output_text)
        return type("DelegateCompleted", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(codex_exec, "_bin", return_value="codex-fixture"), \
            patch("spendguard.config.lane_exec_env", return_value={}), \
            patch.object(codex_exec, "_run_cold_codex_with_reaper",
                         side_effect=substrate_completed_process) as cold_runner:
        result = codex_exec._run_agentic_codex_exec(
            SUBSTRATE_TEST_TASK, model=SUBSTRATE_TEST_MODEL,
            timeout=SUBSTRATE_TEST_TIMEOUT_SECONDS, cwd=tmp_path)
    command = cold_runner.call_args.args[0]
    assert codex_exec.CODEX_AGENTIC_APPROVAL_FLAG in command
    assert "--sandbox" not in command
    assert codex_exec._mcp_disable_flags()[0] not in command
    config_values = [command[index + 1] for index, argument in enumerate(command[:-1])
                     if argument == codex_exec.CODEX_CONFIG_OVERRIDE_FLAG]
    expected_read_only_approvals = {
        codex_exec.CODEX_MCP_TOOL_APPROVAL_OVERRIDE_TEMPLATE.format(
            server=server, tool=tool, mode=codex_exec.CODEX_DELEGATE_READ_ONLY_APPROVAL_MODE)
        for server, tools in codex_exec.CODEX_DELEGATE_REQUIRED_MCP_READ_ONLY_TOOLS.items()
        for tool in tools
    }
    assert set(config_values) == expected_read_only_approvals
    assert len(config_values) == len(expected_read_only_approvals)
    assert set(codex_exec.CODEX_DELEGATE_REQUIRED_MCP_READ_ONLY_TOOLS) == set(
        codex_exec.CODEX_DELEGATE_REQUIRED_MCP_SERVERS)
    assert result["transport"] == "codex-exec-mcp"
    assert result["required_mcp_servers"] == list(codex_exec.CODEX_DELEGATE_REQUIRED_MCP_SERVERS)


def test_delegate_self_narrated_sanctioned_equivalent_is_warning_not_failure(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "requires the configured code-intelligence capability", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    narrated_result = {
        "text": (f"{delegate_router.DELEGATE_TOOL_DEGRADED_MARKER}: {SUBSTRATE_TEST_TOOL} MCP unavailable; "
                 "sanctioned CLI equivalent succeeded."),
        "execution_started": True,
    }
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic", return_value=narrated_result), \
            patch.object(delegate_router, "_worktree_changed_files",
                         return_value=[SUBSTRATE_CHANGED_FILE]), \
            patch.object(delegate_router.lane_registry, "lane_spec",
                         return_value={"exec": "codex_exec", "provider": SUBSTRATE_TEST_PROVIDER}), \
            patch.object(delegate_router.pricing, "realtime_cost", return_value=0.0), \
            patch.object(delegate_router, "_measured_claude_overage_ratio", return_value=None), \
            patch.object(delegate_router.guard, "record_decision"), \
            patch.object(delegate_router.guard, "record_saving"):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "executed"
    assert result["changed_files"] == [SUBSTRATE_CHANGED_FILE]
    assert result["warnings"][0]["reason"] == delegate_router.DELEGATE_EXECUTION_DEGRADED_TOOL_ENVELOPE


def test_delegate_structured_required_tool_degradation_with_partial_edit_fails_loud(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "requires the configured code-intelligence tool", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    degraded_result = {"text": (f"{delegate_router.DELEGATE_TOOL_DEGRADED_MARKER}: "
                                f"{SUBSTRATE_TEST_TOOL}: unavailable"),
                       "execution_started": True}
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic", return_value=degraded_result), \
            patch.object(delegate_router, "_worktree_changed_files",
                         return_value=[SUBSTRATE_CHANGED_FILE]), \
            patch.object(delegate_router.lane_registry, "lane_spec",
                         return_value={"exec": "codex_exec", "provider": SUBSTRATE_TEST_PROVIDER}), \
            patch.object(delegate_router.pricing, "realtime_cost", return_value=0.0), \
            patch.object(delegate_router, "_measured_claude_overage_ratio", return_value=None), \
            patch.object(delegate_router.guard, "record_decision"), \
            patch.object(delegate_router.guard, "record_saving"):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "error"
    assert result["reason"] == delegate_router.DELEGATE_EXECUTION_DEGRADED_TOOL_ENVELOPE
    assert result["degraded_tools"] == [{"tool": SUBSTRATE_TEST_TOOL, "error": "unavailable"}]


def test_delegate_structured_required_tool_degradation_without_work_fails_loud(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "requires the configured code-intelligence tool", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    degraded_result = {"text": (f"{delegate_router.DELEGATE_TOOL_DEGRADED_MARKER}: "
                                f"{SUBSTRATE_TEST_TOOL}: approval denied"),
                       "execution_started": True}
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic", return_value=degraded_result):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "error"
    assert result["reason"] == delegate_router.DELEGATE_EXECUTION_DEGRADED_TOOL_ENVELOPE
    assert result["degraded_tools"] == [{"tool": SUBSTRATE_TEST_TOOL, "error": "approval denied"}]


def test_delegate_degradation_with_non_completion_prose_without_work_fails_loud(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "requires the configured code-intelligence tool", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    degraded_result = {
        "text": (f"{delegate_router.DELEGATE_TOOL_DEGRADED_MARKER}: "
                 f"{SUBSTRATE_TEST_TOOL}: unavailable\n{SUBSTRATE_NON_COMPLETION_RESULT}"),
        "execution_started": True,
    }
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic", return_value=degraded_result), \
            patch.object(delegate_router, "_worktree_changed_files", return_value=[]), \
            patch.object(delegate_router.lane_registry, "lane_spec",
                         return_value={"exec": "codex_exec", "provider": SUBSTRATE_TEST_PROVIDER}), \
            patch.object(delegate_router.pricing, "realtime_cost", return_value=0.0), \
            patch.object(delegate_router, "_measured_claude_overage_ratio", return_value=None), \
            patch.object(delegate_router.guard, "record_decision"), \
            patch.object(delegate_router.guard, "record_saving"):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "error"
    assert result["reason"] == delegate_router.DELEGATE_EXECUTION_DEGRADED_TOOL_ENVELOPE
    assert result["degraded_tools"] == [{"tool": SUBSTRATE_TEST_TOOL, "error": "unavailable"}]


def test_delegate_ordinary_success_without_degradation_stays_successful(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "self-contained fixture", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic",
                         return_value={"text": "completed result", "execution_started": True}), \
            patch.object(delegate_router.lane_registry, "lane_spec",
                         return_value={"exec": "codex_exec", "provider": SUBSTRATE_TEST_PROVIDER}), \
            patch.object(delegate_router.pricing, "realtime_cost", return_value=0.0), \
            patch.object(delegate_router, "_measured_claude_overage_ratio", return_value=None), \
            patch.object(delegate_router.guard, "record_decision"), \
            patch.object(delegate_router.guard, "record_saving"):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "executed"
    assert "warnings" not in result


def test_delegate_overlong_result_fails_without_truncating_execution_evidence(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "self-contained fixture", "fingerprint": "fixture"}
    estimate = {"plan": SUBSTRATE_TEST_PLAN, "model": SUBSTRATE_TEST_MODEL,
                "input_tokens": 1, "output_tokens": 1, "real_api_usd": 0.0}
    complete_lines = [f"evidence line {index}"
                      for index in range(delegate_router.DELEGATE_EXECUTION_RESULT_MAX_LINES + 1)]
    complete_text = "\n".join(complete_lines)
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [SUBSTRATE_TEST_PLAN], "unavailable": []}), \
            patch.object(delegate_router, "_estimate_route", return_value=estimate), \
            patch.object(delegate_router, "_route_agentic",
                         return_value={"text": complete_text, "execution_started": True}):
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "error"
    assert result["reason"] == delegate_router.DELEGATE_EXECUTION_RESULT_TOO_LONG
    assert result["execution"]["text"] == complete_text


def test_delegate_lane_down_fails_closed_without_execution(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CODEX, "kind": "agentic",
                      "provider": SUBSTRATE_TEST_PLAN, "self_contained": True,
                      "why": "self-contained fixture", "fingerprint": "fixture"}
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "delegation_lanes_ready",
                         return_value={"ready": [], "unavailable": [SUBSTRATE_TEST_PLAN]}), \
            patch.object(delegate_router, "_route_agentic") as executor:
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "refused"
    executor.assert_not_called()


def test_delegate_timeout_fails_fast_with_typed_error(tmp_path):
    with patch.object(codex_exec, "_bin", return_value="codex-fixture"), \
            patch("spendguard.config.lane_exec_env", return_value={}), \
            patch.object(codex_exec, "_run_cold_codex_with_reaper",
                         side_effect=subprocess.TimeoutExpired("codex-fixture", SUBSTRATE_TEST_TIMEOUT_SECONDS)):
        result = codex_exec._run_agentic_codex_exec(
            SUBSTRATE_TEST_TASK, model=SUBSTRATE_TEST_MODEL,
            timeout=SUBSTRATE_TEST_TIMEOUT_SECONDS, cwd=tmp_path)
    assert result["execution_started"] is True
    assert result["error"] == (
        f"codex delegated execution timed out after {SUBSTRATE_TEST_TIMEOUT_SECONDS} seconds")


def test_delegate_claude_route_is_a_brief_defect_without_execution(tmp_path):
    classification = {"route": delegate_router.DELEGATE_ROUTE_CLAUDE_SUBAGENT, "kind": "agentic",
                      "provider": None, "self_contained": False,
                      "why": "requires the host's live conversation", "fingerprint": "fixture"}
    with patch.object(delegate_router, "classify_task", return_value=classification), \
            patch.object(delegate_router, "_route_agentic") as executor:
        result = delegate_router.delegate_task(SUBSTRATE_TEST_TASK, execute=True, cwd=tmp_path)
    assert result["status"] == "refused"
    assert result["brief_defect"] is True
    assert result["reason"] == "delegate_brief_not_self_contained"
    executor.assert_not_called()


def test_delegate_cwd_provider_and_timeout_are_applied(tmp_path):
    with patch.object(delegate_router.lane_registry, "lane_spec",
                      return_value={"exec": "codex_exec", "provider": SUBSTRATE_TEST_PROVIDER}), \
            patch.object(codex_exec, "run_prompt",
                         return_value={"text": "done", "execution_started": True}) as runner:
        delegate_router._route_agentic(SUBSTRATE_TEST_PLAN, SUBSTRATE_TEST_TASK,
                                       SUBSTRATE_TEST_MODEL, SUBSTRATE_TEST_TIMEOUT_SECONDS, tmp_path)
    arguments = runner.call_args.kwargs
    assert arguments["cwd"] == tmp_path
    assert arguments["model"] == SUBSTRATE_TEST_MODEL
    assert arguments["timeout"] == SUBSTRATE_TEST_TIMEOUT_SECONDS


if __name__ == "__main__":
    test_delegate_route_classifier_uses_model_for_every_label()
    test_delegate_classifier_cache_is_content_addressed()
    with tempfile.TemporaryDirectory() as substrate_test_directory:
        substrate_test_path = Path(substrate_test_directory)
        test_codex_delegate_enables_mcp_and_scoped_automatic_review(substrate_test_path)
        test_delegate_self_narrated_sanctioned_equivalent_is_warning_not_failure(substrate_test_path)
        test_delegate_structured_required_tool_degradation_with_partial_edit_fails_loud(substrate_test_path)
        test_delegate_structured_required_tool_degradation_without_work_fails_loud(substrate_test_path)
        test_delegate_degradation_with_non_completion_prose_without_work_fails_loud(substrate_test_path)
        test_delegate_ordinary_success_without_degradation_stays_successful(substrate_test_path)
        test_delegate_lane_down_fails_closed_without_execution(substrate_test_path)
        test_delegate_cwd_provider_and_timeout_are_applied(substrate_test_path)
    print("OK test_delegate_subagent_substrate")
