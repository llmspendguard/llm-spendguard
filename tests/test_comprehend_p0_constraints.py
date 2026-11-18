"""P0 regressions: incremental comprehend persistence and fail-closed bulk constraints."""
import json
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import spendguard
from spendguard import comprehend, lane_balance
from spendguard import dispatch as bulk_dispatch


def test_comprehend_persists_completed_verdicts_before_terminal_failure(monkeypatch, tmp_path):
    item_paths = []
    for item_number in range(3):
        item_path = tmp_path / f"item-{item_number}.txt"
        item_path.write_text(f"evidence-{item_number}")
        item_paths.append(str(item_path))
    output_path = tmp_path / "partial.jsonl"
    checkpoint_path = tmp_path / "fallback.jsonl"
    monkeypatch.setattr(spendguard, "require", lambda: None)
    monkeypatch.setattr(comprehend.config, "scoped_intent_cap", lambda *args: nullcontext())
    monkeypatch.setattr(comprehend.calls, "context", lambda **kwargs: nullcontext())
    monkeypatch.setattr(comprehend, "estimate_corpus", lambda tasks, model, intent: {
        "model": model, "tasks": len(tasks), "in_tok": len(tasks), "out_tok_per_task": 1,
        "out_basis": "fixture", "est_usd_ceiling": 0})

    def fail_after_two_completed_items(live, intent, selected_lanes, checkpoint, refuse_billed, panel, model,
                                       lanes_or_default=False, settled_result_callback=None):
        for index in range(2):
            row = {"text": f"verdict-{index}", "lane": "fixture-lane", "billed": False}
            with open(checkpoint, "a") as checkpoint_stream:
                checkpoint_stream.write(json.dumps({"k": live[index]["key"], "r": row}) + "\n")
                checkpoint_stream.flush()
            settled_result_callback(index, row)
        raise RuntimeError("fixture terminal failure")

    monkeypatch.setattr(comprehend, "_comprehend_dispatch_tasks", fail_after_two_completed_items)
    with pytest.raises(RuntimeError, match="terminal failure"):
        comprehend.comprehend_corpus(items=item_paths, intent="partial-persistence-fixture", model="fixture-model",
                                     run=True, checkpoint=str(checkpoint_path), out=str(output_path))
    output_records = [json.loads(line) for line in output_path.read_text().splitlines()]
    checkpoint_records = [json.loads(line) for line in checkpoint_path.read_text().splitlines()]
    assert [record["kind"] for record in output_records] == ["verdict", "verdict", "gap"]
    assert len(checkpoint_records) == 2
    assert output_records[-1]["unfinished"] == item_paths[2:]


def test_pinned_bulk_model_refuses_ineligible_lane_arm(monkeypatch):
    adapter_calls = []
    monkeypatch.setattr(lane_balance, "_bulk_arms", lambda intent, lanes=None: [("fixture-lane", "other-model")])
    monkeypatch.setattr(lane_balance._reference_data_accessors, "resolve_lane_provider", lambda lane: "fixture-provider")
    monkeypatch.setattr(lane_balance._execution_accessors, "resolve__lane_cooling", lambda lane: False)
    monkeypatch.setattr(lane_balance._execution_accessors, "resolve_call",
                        lambda *args, **kwargs: adapter_calls.append((args, kwargs)))
    monkeypatch.setattr("spendguard.lane_economics.prompt_lane_reserved", lambda lane: False)
    result = lane_balance.bulk_delegate(["task"], "pin-fixture", lanes=["fixture-lane"],
                                        pinned_model="requested-model", force=True)
    assert result[0]["reason"] == "pinned_model_unavailable"
    assert "requested-model" in result[0]["error"]
    assert adapter_calls == []


def test_refuse_billed_advances_to_next_free_lane(monkeypatch):
    attempted_models = []
    arms = [("lane-one", "model-one"), ("lane-two", "model-two")]
    monkeypatch.setattr(lane_balance, "_bulk_arms", lambda intent, lanes=None: list(arms))
    monkeypatch.setattr(lane_balance._reference_data_accessors, "resolve_lane_provider", lambda lane: "provider")
    monkeypatch.setattr(lane_balance._execution_accessors, "resolve__lane_cooling", lambda lane: False)
    monkeypatch.setattr("spendguard.lane_economics.prompt_lane_reserved", lambda lane: False)
    monkeypatch.setattr(bulk_dispatch, "acquire_or_none", lambda *args, **kwargs: 0.0)
    monkeypatch.setattr(bulk_dispatch, "release", lambda *args, **kwargs: None)
    monkeypatch.setattr(bulk_dispatch, "lane_free", lambda lane: 1)

    def serve_second_free_lane(model, prompt, **kwargs):
        attempted_models.append(model)
        if model.endswith("model-one"):
            return {"text": None, "error": "miss", "reason": "lane_error", "executor": "lane-one"}
        return {"text": "served", "error": None, "reason": None, "executor": "lane-two",
                "provider": "provider", "model": "model-two", "cost": 0.0}

    monkeypatch.setattr(lane_balance._execution_accessors, "resolve_call", serve_second_free_lane)
    result = lane_balance.bulk_delegate(["task"], "free-retry-fixture", refuse_billed=True, force=True)
    assert result[0]["text"] == "served"
    assert attempted_models == ["provider:model-one", "provider:model-two"]


def test_comprehend_cli_refusal_without_output_is_nonzero(monkeypatch, tmp_path):
    item_path = tmp_path / "item.txt"
    item_path.write_text("evidence")
    monkeypatch.setattr(comprehend, "comprehend_corpus", lambda *args, **kwargs: {
        "files": 1, "tasks": 1, "unreadable": [], "estimate": {"model": "fixture", "est_usd_ceiling": 0,
        "in_tok": 1, "out_tok_per_task": 1, "out_basis": "fixture"}, "ran": True, "served": 0,
        "results": {str(item_path): {"lanes": [], "billed": False}}, "billed_files": [],
        "checkpoint": str(tmp_path / "checkpoint")})
    assert comprehend.cmd(["--items", str(item_path), "--intent", "refusal-fixture", "--run",
                           "--refuse-billed"]) != 0


if __name__ == "__main__":
    standalone_checks = (
        test_comprehend_persists_completed_verdicts_before_terminal_failure,
        test_pinned_bulk_model_refuses_ineligible_lane_arm,
        test_refuse_billed_advances_to_next_free_lane,
        test_comprehend_cli_refusal_without_output_is_nonzero,
    )
    for standalone_check in standalone_checks:
        with TemporaryDirectory() as temporary_directory, pytest.MonkeyPatch.context() as standalone_patches:
            parameters = [standalone_patches]
            if "tmp_path" in standalone_check.__code__.co_varnames[:standalone_check.__code__.co_argcount]:
                parameters.append(Path(temporary_directory))
            standalone_check(*parameters)
    print(f"comprehend P0 constraints: {len(standalone_checks)} checks passed")
