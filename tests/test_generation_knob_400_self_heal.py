"""Offline regression proof for typed generation-knob 400 healing."""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

TEST_ISOLATION_FLAG = "SPENDGUARD_TEST_ISOLATED"
TEST_HOME_VARIABLE = "SPENDGUARD_HOME"
TEST_MODEL = "gpt-6-luna"
TEST_PROMPT = "Return an answer."
TEST_INTENT = "generation-knob-400-self-heal"
GENERATION_KNOBS = ("temperature", "top_p", "seed")
UNSUPPORTED_KNOB_VALUE = 0
PROVIDER_REJECTION_TEMPLATE = (
    "Unsupported value: '{knob}' does not support {value} with this model. "
    "Only the default value is supported."
)

if not os.environ.get(TEST_ISOLATION_FLAG):
    os.environ[TEST_ISOLATION_FLAG] = "1"
    os.environ[TEST_HOME_VARIABLE] = tempfile.mkdtemp(prefix="sg-gen-knob-heal-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard  # noqa: E402

spendguard.require = lambda: None

from spendguard import adapters, models  # noqa: E402


class SimulatedBadRequestError(Exception):
    """OpenAI-compatible 400 shape: the rejected parameter is a typed field."""

    def __init__(self, knob, value):
        super().__init__(PROVIDER_REJECTION_TEMPLATE.format(knob=knob, value=value))
        self.param = knob
        self.status_code = 400


def _successful_openai_reply():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
    )


ISOLATED_MODEL_FACTS = {}
models.facts = lambda model: ISOLATED_MODEL_FACTS.get(model, {})
models.add_fact = lambda model, key, value, **metadata: ISOLATED_MODEL_FACTS.setdefault(
    model, {}).__setitem__(
        key,
        (value, metadata.get("confidence"), metadata.get("source"), metadata.get("verified")),
    )


def verify_typed_generation_knob_400_learns_drops_and_persists(knob):
    ISOLATED_MODEL_FACTS.clear()
    captured_bodies = []

    def reject_until_knob_is_absent(**body):
        captured_bodies.append(body)
        if knob in body:
            raise SimulatedBadRequestError(knob, body[knob])
        return _successful_openai_reply()

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=reject_until_knob_is_absent)))
    call_kwargs = {
        knob: UNSUPPORTED_KNOB_VALUE,
        "max_tokens": adapters.TOKEN_FLOOR,
        "intent": TEST_INTENT,
        "measurement": True,
        "metered_only": True,
        "no_substitution": True,
        "_route": False,
        "_no_guard": True,
        "retries": 0,
    }

    import openai

    with patch.object(adapters.config, "api_key", return_value=object()), \
         patch.object(openai, "OpenAI", return_value=fake_client):
        first_result = adapters.call(TEST_MODEL, TEST_PROMPT, **call_kwargs)

        assert not first_result.get("error"), first_result
        assert len(captured_bodies) == 2, captured_bodies
        assert knob in captured_bodies[0]
        assert knob not in captured_bodies[1]
        assert all("max_tokens" not in body for body in captured_bodies), captured_bodies
        assert all("max_completion_tokens" in body for body in captured_bodies), captured_bodies
        assert first_result["gen_params_dropped"][knob] == UNSUPPORTED_KNOB_VALUE
        assert f"gen_unsupported:{knob}" in ISOLATED_MODEL_FACTS[TEST_MODEL]

        captured_bodies.clear()
        second_result = adapters.call(TEST_MODEL, TEST_PROMPT, **call_kwargs)

    assert not second_result.get("error"), second_result
    assert len(captured_bodies) == 1, captured_bodies
    assert knob not in captured_bodies[0]
    assert "max_completion_tokens" in captured_bodies[0]
    assert "max_tokens" not in captured_bodies[0]
    assert second_result["gen_params_dropped"][knob] == UNSUPPORTED_KNOB_VALUE


if __name__ == "__main__":
    for selected_knob in GENERATION_KNOBS:
        verify_typed_generation_knob_400_learns_drops_and_persists(selected_knob)
    print("OK generation knob 400 self-heal: temperature, top_p, seed + persisted upfront drop")
