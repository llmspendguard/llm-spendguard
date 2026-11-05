"""Verify Bedrock capture uses dependency-free, eagerly wired recorder seams."""
import ast
import os
from types import SimpleNamespace
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _pytest_fixture_bridge import align_test_startup_home  # noqa: E402

align_test_startup_home(prefix=__name__)

import spendguard  # noqa: E402,F401
from spendguard.generate.spend_capture import bedrock_adapter  # noqa: E402
from spendguard.manage.admission import budget, budget_unpriced_sink, gate  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "manage", "admission", "budget_unpriced_sink.py",
)


def _budget_unpriced_sink_leaf_import_targets():
    """Return every leaf import as (is_relative, module_name), parsed from the full source."""
    with open(LEAF_SOURCE_PATH, "r", encoding="utf-8") as leaf_file:
        tree = ast.parse(leaf_file.read(), filename=LEAF_SOURCE_PATH)
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((False, alias.name))
        elif isinstance(node, ast.ImportFrom):
            targets.append((node.level > 0, node.module or ""))
    return targets


def main():
    for is_relative, module_name in _budget_unpriced_sink_leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r})"
        assert not module_name.startswith("spendguard"), f"leaf imports spendguard module {module_name!r}"

    assert budget_unpriced_sink._unpriced_recorder is budget.record_unpriced
    assert bedrock_adapter.record_unpriced_event is budget_unpriced_sink.record_unpriced_event
    assert not hasattr(bedrock_adapter, "budget")
    assert bedrock_adapter._bedrock_realtime_recorder is gate._record_rt
    assert not hasattr(bedrock_adapter, "gate")

    original_unpriced_recorder = budget_unpriced_sink._unpriced_recorder
    try:
        budget_unpriced_sink._unpriced_recorder = None
        try:
            budget_unpriced_sink.record_unpriced_event("provider", "model", "kind")
        except RuntimeError:
            pass
        else:
            raise AssertionError("record_unpriced_event must fail loudly when its recorder is unset")
    finally:
        budget_unpriced_sink._unpriced_recorder = original_unpriced_recorder

    realtime_calls = []
    unpriced_calls = []

    def capture_realtime(*args, **kwargs):
        realtime_calls.append((args, kwargs))

    def capture_unpriced(*args, **kwargs):
        unpriced_calls.append((args, kwargs))

    client = SimpleNamespace(meta=SimpleNamespace(service_model=SimpleNamespace(service_name="bedrock-runtime")))
    converse_response = {"usage": {"inputTokens": 11, "outputTokens": 7}}
    stream_response = {"stream": object()}
    responses = {"Converse": converse_response, "ConverseStream": stream_response}

    def original_api_call(_client, operation_name, _api_params):
        return responses[operation_name]

    wrapped_api_call = bedrock_adapter._wrap(original_api_call)
    try:
        bedrock_adapter.set_bedrock_realtime_recorder(capture_realtime)
        budget_unpriced_sink.set_unpriced_recorder(capture_unpriced)
        returned_converse_response = wrapped_api_call(client, "Converse", {"modelId": "model"})
        returned_stream_response = wrapped_api_call(client, "ConverseStream", {"modelId": "model"})

        assert returned_converse_response is converse_response
        assert returned_stream_response is stream_response
        assert realtime_calls == [
            (("model", {"model": "model"}, 11, 7), {"provider": "bedrock"}),
        ]
        assert unpriced_calls == [
            (("bedrock", "model", "realtime"), {"in_tok": 0, "out_tok": 0, "project": None}),
        ]
    finally:
        bedrock_adapter.set_bedrock_realtime_recorder(gate._record_rt)
        budget_unpriced_sink.set_unpriced_recorder(budget.record_unpriced)


def test_bedrock_capture_leaf():
    main()


if __name__ == "__main__":
    main()
