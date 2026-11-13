"""Caller cap aliases cannot override the catalog output budget; deprecation is once-only."""
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch

import spendguard

spendguard.require = lambda: None
from spendguard import adapters, models  # noqa: E402

CALLER_CAP_DEPRECATION_MODEL = "gpt-6-luna"
CALLER_CAP_DEPRECATION_PROMPT = "Return an answer."
CALLER_CAP_ARGUMENT_NAMES = ("max_tokens", "max_completion_tokens", "max_output_tokens")
CALLER_CAP_EXPECTED_WARNING_COUNT = 1


def verify_caller_cap_deprecation_authority():
    import openai

    bodies = []
    notices = []
    expected_cap = adapters.output_budget(CALLER_CAP_DEPRECATION_MODEL)
    caller_cap = expected_cap // len(CALLER_CAP_ARGUMENT_NAMES)
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))

    def capture_deprecated_cap_body(**body):
        bodies.append(body)
        return reply

    real_warn_once = adapters.config.warn_once

    def capture_deprecated_cap_notice(message, *args, **kwargs):
        notices.append(message)
        return real_warn_once(message, *args, **kwargs)

    with contextlib.redirect_stderr(io.StringIO()) as stderr, \
         patch.object(adapters.config, "warn_once", side_effect=capture_deprecated_cap_notice), \
         patch.object(adapters.config, "api_key", return_value=object()), \
         patch.object(openai, "OpenAI", return_value=SimpleNamespace(chat=SimpleNamespace(
             completions=SimpleNamespace(create=capture_deprecated_cap_body)))):
        for cap_name in CALLER_CAP_ARGUMENT_NAMES:
            result = adapters.call(CALLER_CAP_DEPRECATION_MODEL, CALLER_CAP_DEPRECATION_PROMPT,
                                   measurement=True, metered_only=True, **{cap_name: caller_cap})
            assert not result.get("error"), result
    for body in bodies:
        assert body[models.tokens_param(CALLER_CAP_DEPRECATION_MODEL, dialect="openai")] == expected_cap, body
        assert "max_tokens" not in body, body
    cap_notices = [notice for notice in notices if "deprecated" in notice.lower()]
    assert cap_notices, "caller cap must emit a deprecation through config.warn_once"
    assert stderr.getvalue().lower().count("deprecated") == CALLER_CAP_EXPECTED_WARNING_COUNT, stderr.getvalue()
    assert "ignored" in stderr.getvalue().lower(), stderr.getvalue()
    print("OK caller output cap ignored under catalog name; deprecation emitted exactly once")


if __name__ == "__main__":
    verify_caller_cap_deprecation_authority()
