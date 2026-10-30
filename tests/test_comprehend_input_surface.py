"""Offline targeting guards for literal inputs, vendor confinement and pinned panel fan-out."""
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import spendguard
from spendguard import comprehend


def comprehend_surface_harness(monkeypatch, tmp_path):
    observed = []
    providers = {"lane_a": "vendor_a", "lane_b": "vendor_b"}
    monkeypatch.setattr(comprehend.lane_catalog, "lanes", lambda: list(providers))
    monkeypatch.setattr(comprehend.lane_catalog, "lane_provider", providers.get)
    monkeypatch.setattr(comprehend.lane_catalog, "configured_base", lambda lane: lane + "_model")
    monkeypatch.setattr(comprehend, "estimate_corpus", lambda tasks, model, intent: {
        "model": model, "tasks": len(tasks), "in_tok": sum(t["chars"] for t in tasks),
        "out_tok_per_task": 1, "out_basis": "test", "est_usd_ceiling": len(tasks)})
    monkeypatch.setattr(spendguard, "require", lambda: None)
    monkeypatch.setattr(comprehend.config, "scoped_intent_cap", lambda *args: nullcontext())
    monkeypatch.setattr(comprehend.calls, "context", lambda **kwargs: nullcontext())

    def capture_comprehend_surface_fan(tasks, **kwargs):
        observed.append((tasks, kwargs))
        keys = kwargs["task_key"]
        return {(keys(task) if callable(keys) else keys[index]): {
            "text": kwargs["model_for"](task) if "model_for" in kwargs else "answer",
            "lane": kwargs["lanes"][index % len(kwargs["lanes"])] if kwargs["lanes"] else "lane_a"}
                for index, task in enumerate(tasks)}

    monkeypatch.setattr(comprehend.lane_balance, "bulk_delegate", capture_comprehend_surface_fan)
    first = tmp_path / "literal[identity].txt"
    first.write_text("evidence begins\n" + "whole evidence\n" * 20 + "late finding")
    other = tmp_path / "unselected.txt"
    other.write_text("must never enter prompt")
    return first, other, observed, tmp_path / "checkpoint.jsonl"


def test_comprehend_literal_items_exact_and_whole(comprehend_surface_harness):
    first, other, observed, checkpoint = comprehend_surface_harness
    result = comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model",
                                          run=True, checkpoint=str(checkpoint), max_chars=len(first.read_text()))
    assert set(result["results"]) == {str(first)}
    assert len(observed[0][0]) == 1
    assert observed[0][0][0].endswith(first.read_text())
    assert other.read_text() not in observed[0][0][0]
    with pytest.raises(ValueError, match="alternative"):
        comprehend.comprehend_corpus([str(other)], items=[str(first)], intent="surface-test")
    observed.clear()
    comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model", run=True,
                                 checkpoint=str(checkpoint), max_chars=len("evidence begins\n"))
    prompts = observed[0][0]
    evidence = "".join(prompt.split("---\n", 1)[1] for prompt in prompts)
    assert evidence == first.read_text()


def test_comprehend_vendor_confinement(comprehend_surface_harness):
    first, _, observed, checkpoint = comprehend_surface_harness
    comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model",
                                 vendors=["vendor_b"], run=True, checkpoint=str(checkpoint))
    assert observed[0][1]["lanes"] == ["lane_b"]
    with pytest.raises(ValueError, match="no lanes"):
        comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model",
                                     vendors=["vendor_b"], lanes=["lane_a"])
    with pytest.raises(ValueError, match="no lanes"):
        comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model", vendors=[])
    with pytest.raises(ValueError, match="unknown lane vendors"):
        comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model", vendors=["unknown"])


def test_comprehend_panel_matrix_and_estimate(comprehend_surface_harness):
    first, _, observed, checkpoint = comprehend_surface_harness
    result = comprehend.comprehend_corpus(items=[str(first)], intent="surface-test", model="test-model",
                                          panel=True, run=True, checkpoint=str(checkpoint))
    tasks, controls = observed[0]
    assert len(tasks) == 2
    assert {controls["model_for"](task) for task in tasks} == {
        "vendor_a:lane_a_model", "vendor_b:lane_b_model"}
    assert len({controls["task_key"](task) for task in tasks}) == len(tasks)
    assert all(controls["prompt_for"](task).endswith(first.read_text()) for task in tasks)
    assert result["tasks"] == result["served"] == 2
    assert result["estimate"]["est_usd_ceiling"] == 2
    assert set(result["results"][str(first)]["reviews"]) == {"vendor_a", "vendor_b"}


def test_comprehend_generated_cli_flags_reach_core(comprehend_surface_harness):
    first, _, observed, checkpoint = comprehend_surface_harness
    assert comprehend.cmd(["--items", str(first), "--vendors", "vendor_b", "--panel", "--intent",
                           "surface-test", "--model", "test-model", "--run", "--checkpoint", str(checkpoint)]) == 0
    tasks, controls = observed[0]
    assert len(tasks) == 1
    assert controls["model_for"](tasks[0]) == "vendor_b:lane_b_model"
    with pytest.raises(SystemExit):
        comprehend.cmd([str(first), "--items", str(first), "--intent", "surface-test"])


if __name__ == "__main__":
    checks = (test_comprehend_literal_items_exact_and_whole, test_comprehend_vendor_confinement,
              test_comprehend_panel_matrix_and_estimate, test_comprehend_generated_cli_flags_reach_core)
    for check in checks:
        with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patches:
            check(comprehend_surface_harness(patches, Path(directory)))
    print(f"comprehend input surface: {len(checks)} checks passed")
