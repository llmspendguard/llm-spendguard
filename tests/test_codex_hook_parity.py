"""Offline native Codex payload contract; no provider calls."""
import json
import os
import pathlib
import tempfile
import uuid

from spendguard import agent_spawn_gate, codex, residency_gate


def test_codex_native_hook_contract():
    os.environ["SPENDGUARD_HOOK_HOST"] = "codex"
    session_id = str(uuid.uuid4())
    model = codex._DEFAULT_MODEL
    token_count = len(session_id)
    message = session_id + str(uuid.uuid4())
    task_name = str(uuid.uuid4())
    payload = {"session_id": session_id, "tool_input": {"message": message, "task_name": task_name}}
    assert agent_spawn_gate._spawn_fields(payload) == (message, task_name)
    assert agent_spawn_gate._hosting_lane() == "codex"
    records = [
        {"type": "session_meta", "payload": {"id": session_id}},
        {"type": "turn_context", "payload": {"turn_id": task_name, "model": model}},
        {"type": "turn_context", "payload": {"turn_id": task_name, "model": model}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": token_count}}}},
    ]
    with tempfile.TemporaryDirectory() as temporary:
        transcript = pathlib.Path(temporary) / f"{session_id}.jsonl"
        transcript.write_text("\n".join(json.dumps(record) for record in records))
        payload["transcript_path"] = str(transcript)
        assert codex.codex_hook_context_trajectory(payload) == {
            "turns": len({task_name}), "model": model, "current": token_count}
        original_band = agent_spawn_gate._plan_in_band
        original_classifier = agent_spawn_gate.delegate_router.classify_task
        observed = []
        agent_spawn_gate._plan_in_band = lambda: True
        def codex_parity_classifier(prompt, **options):
            observed.append((prompt, options))
            return {"kind": "oneshot", "why": task_name}
        agent_spawn_gate.delegate_router.classify_task = codex_parity_classifier
        try:
            decision = agent_spawn_gate.evaluate_pretooluse(payload)
            assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert observed == [(message, {"exclude_plans": ["codex"]})]
        finally:
            agent_spawn_gate._plan_in_band = original_band
            agent_spawn_gate.delegate_router.classify_task = original_classifier
        original_threshold = residency_gate.residency_nudge_thresholds_met
        original_judge = residency_gate._judge_residency_delegability
        residency_gate.residency_nudge_thresholds_met = lambda *args: True
        residency_gate._judge_residency_delegability = lambda *args: {"delegable": True, "why": task_name}
        payload["tool_response"] = message
        try:
            assert task_name in residency_gate.evaluate_posttooluse(payload)["systemMessage"]
        finally:
            residency_gate.residency_nudge_thresholds_met = original_threshold
            residency_gate._judge_residency_delegability = original_judge
        payload["session_id"] = str(uuid.uuid4())
        try:
            codex.codex_hook_context_trajectory(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("Cross-session evidence must be refused")
        assert residency_gate.evaluate_posttooluse({}) == {}
    os.environ.pop("SPENDGUARD_HOOK_HOST")


if __name__ == "__main__":
    test_codex_native_hook_contract()
    print("Codex native payload, hosting lane, transcript identity, and named gaps passed; zero provider calls")
