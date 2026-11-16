"""Offline wire-body proof: cap spelling follows the catalog, independently of reasoning."""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

TEST_ISOLATION_FLAG = "SPENDGUARD_TEST_ISOLATED"
TEST_HOME_VARIABLE = "SPENDGUARD_HOME"
if not os.environ.get(TEST_ISOLATION_FLAG):
    os.environ[TEST_ISOLATION_FLAG] = "1"
    os.environ[TEST_HOME_VARIABLE] = tempfile.mkdtemp(prefix="sg-cap-wire-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard

spendguard.require = lambda: None

from spendguard import adapters, models  # noqa: E402

CAP_AUTHORITY_LUNA_MODEL = "gpt-6-luna"
CAP_AUTHORITY_ANTHROPIC_MODEL = "claude-opus-4-8"
CAP_AUTHORITY_PROMPT = "Return an answer."
_CAPTURED_WIRE_BODIES = None


def capture_catalog_cap_wire_bodies():
    global _CAPTURED_WIRE_BODIES
    if _CAPTURED_WIRE_BODIES is not None:
        return _CAPTURED_WIRE_BODIES
    import anthropic
    import openai

    wire_bodies = {}
    openai_reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
    )
    anthropic_reply = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="answer")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=0, output_tokens=0,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )

    def capture_catalog_openai_body(**body):
        wire_bodies["openai"] = body
        return openai_reply

    class CatalogCapAnthropicStream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return anthropic_reply

    def capture_catalog_anthropic_body(**body):
        wire_bodies["anthropic"] = body
        return CatalogCapAnthropicStream()

    with patch.object(adapters.config, "api_key", return_value=object()), \
         patch.object(openai, "OpenAI", return_value=SimpleNamespace(chat=SimpleNamespace(
             completions=SimpleNamespace(create=capture_catalog_openai_body)))), \
         patch.object(anthropic, "Anthropic", return_value=SimpleNamespace(messages=SimpleNamespace(
             stream=capture_catalog_anthropic_body))):
        for selected_model in (CAP_AUTHORITY_LUNA_MODEL, CAP_AUTHORITY_ANTHROPIC_MODEL):
            result = adapters.call(selected_model, CAP_AUTHORITY_PROMPT,
                                   intent="catalog-cap-wire-authority", measurement=True,
                                   no_substitution=True, metered_only=True, _route=False,
                                   temperature=0)
            assert not result.get("error"), result
    _CAPTURED_WIRE_BODIES = wire_bodies
    return _CAPTURED_WIRE_BODIES


def test_luna_metered_measurement_body_uses_catalog_cap():
    wire_bodies = capture_catalog_cap_wire_bodies()
    assert "max_completion_tokens" in wire_bodies["openai"], wire_bodies
    assert "max_tokens" not in wire_bodies["openai"], wire_bodies


def test_cap_name_is_independent_of_reasons_by_default():
    assert models.reasons_by_default(CAP_AUTHORITY_LUNA_MODEL) is False
    assert models.tokens_param(CAP_AUTHORITY_LUNA_MODEL, dialect="openai") == "max_completion_tokens"
    wire_bodies = capture_catalog_cap_wire_bodies()
    assert "max_completion_tokens" in wire_bodies["openai"], wire_bodies
    assert "max_tokens" not in wire_bodies["openai"], wire_bodies


def test_anthropic_body_retains_max_tokens():
    wire_bodies = capture_catalog_cap_wire_bodies()
    assert "max_tokens" in wire_bodies["anthropic"], wire_bodies
    assert "max_completion_tokens" not in wire_bodies["anthropic"], wire_bodies


def verify_catalog_cap_wire_authority():
    test_luna_metered_measurement_body_uses_catalog_cap()
    test_cap_name_is_independent_of_reasons_by_default()
    test_anthropic_body_retains_max_tokens()
    print("OK catalog cap wire authority: three wire-body guards")


if __name__ == "__main__":
    verify_catalog_cap_wire_authority()
