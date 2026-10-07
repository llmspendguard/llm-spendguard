"""Offline guards for the shared token parameter authority and batch pre-flight wiring."""
import json
import os
import pathlib
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-batch-preflight-")
os.environ["OPENAI_API_KEY"] = "sk-test-offline-openai"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-offline-anthropic"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import gate, models, submit  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


print("-- tokens_param is the single output-budget parameter authority --")
for _model in ("gpt-6.1-sol", "gpt-7-foo", "kimi-k3", "glm-5.2"):
    check(f"{_model}: default dialect uses max_completion_tokens",
          models.tokens_param(_model) == "max_completion_tokens")
    check(f"{_model}: OpenAI dialect uses max_completion_tokens",
          models.tokens_param(_model, dialect="openai") == "max_completion_tokens")
check("Claude defaults to max_tokens", models.tokens_param("claude-opus-4-8") == "max_tokens")
check("Anthropic dialect forces max_tokens",
      models.tokens_param("gpt-6.1-sol", dialect="anthropic") == "max_tokens")
models.add_fact("some-test-model", "tokens_param", "max_tokens", source="test")
check("a measured per-model fact overrides the OpenAI dialect default",
      models.tokens_param("some-test-model", dialect="openai") == "max_tokens")

_params = models.apply_call_params("gpt-6.1-sol", {"max_tokens": 1000}, dialect="openai")
check("apply_call_params renames max_tokens for a future GPT family",
      _params == {"max_completion_tokens": 1000}, f"params={_params!r}")


class _Obj:
    def __init__(self, object_id):
        self.id = object_id


class _OpenAIFiles:
    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(("files.create", kwargs))
        return _Obj("file-test")


class _OpenAIBatches:
    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(("batches.create", kwargs))
        return _Obj("batch-openai-test")


class _FakeOpenAI:
    calls = []

    def __init__(self, **kwargs):
        self.files = _OpenAIFiles(self.calls)
        self.batches = _OpenAIBatches(self.calls)


class _AnthropicBatches:
    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(("batches.create", kwargs))
        return _Obj("batch-anthropic-test")


class _AnthropicMessages:
    def __init__(self, calls):
        self.batches = _AnthropicBatches(calls)


class _FakeAnthropic:
    calls = []

    def __init__(self, **kwargs):
        self.messages = _AnthropicMessages(self.calls)


print("-- guarded_submit refuses before booking or creating, then proceeds after a passing pre-flight --")
import anthropic  # noqa: E402
import openai  # noqa: E402

_orig_preflight = submit._preflight_first_request
_orig_openai = openai.OpenAI
_orig_anthropic = anthropic.Anthropic
_orig_record = gate.record_accepted_batch_estimate
_orig_estimate_jsonl = submit.estimate_jsonl_cost
_bookings = []
gate.record_accepted_batch_estimate = lambda est: _bookings.append(est)
submit.estimate_jsonl_cost = lambda *args, **kwargs: {
    "requests": 1, "in_tok": 1, "out_tok": 16, "cost": 0.01, "media": False,
    "media_units": 0, "out_basis": "test", "token_basis": "test", "mode": "batch",
    "model": "gpt-6.1-sol",
}
try:
    with tempfile.TemporaryDirectory() as _tmp:
        _jsonl = pathlib.Path(_tmp) / "chat.jsonl"
        _jsonl.write_text(json.dumps({
            "custom_id": "t0", "method": "POST", "url": "/v1/chat/completions",
            "body": {"model": "gpt-6.1-sol", "max_completion_tokens": 16,
                     "messages": [{"role": "user", "content": "hi"}]},
        }) + "\n")
        submit._preflight_first_request = lambda *args: {
            "ok": False, "error": "Unsupported parameter: 'max_tokens' is not supported", "hint": "",
        }
        openai.OpenAI = _FakeOpenAI
        _FakeOpenAI.calls.clear()
        _raised = None
        try:
            submit.guarded_submit(str(_jsonl), model="gpt-6.1-sol", cap_dollars=1000.0,
                                  submit=True, endpoint="/v1/chat/completions")
        except RuntimeError as _exc:
            _raised = str(_exc)
        check("failed OpenAI pre-flight raises PRE-FLIGHT FAILED",
              _raised is not None and "PRE-FLIGHT FAILED" in _raised, f"error={_raised!r}")
        check("failed OpenAI pre-flight books no estimate", not _bookings, f"bookings={_bookings!r}")
        check("failed OpenAI pre-flight never calls batches.create", not _FakeOpenAI.calls,
              f"calls={_FakeOpenAI.calls!r}")

        submit._preflight_first_request = lambda *args: {"ok": True}
        _FakeOpenAI.calls.clear()
        _batch_id = submit.guarded_submit(str(_jsonl), model="gpt-6.1-sol", cap_dollars=1000.0,
                                          submit=True, endpoint="/v1/chat/completions")
        check("passing OpenAI pre-flight proceeds to batches.create",
              any(name == "batches.create" for name, _ in _FakeOpenAI.calls),
              f"calls={_FakeOpenAI.calls!r}")
        check("guarded_submit returns the created OpenAI batch id", _batch_id == "batch-openai-test")

    print("-- submit_message_batch has the same fail-closed pre-flight ordering --")
    _bookings.clear()
    submit._preflight_first_request = lambda *args: {
        "ok": False, "error": "Unsupported parameter", "hint": "",
    }
    anthropic.Anthropic = _FakeAnthropic
    _FakeAnthropic.calls.clear()
    _failed = submit.submit_message_batch(["hi"], model="claude-opus-4-8", submit=True,
                                          cap_dollars=1000.0, expected_out_tokens=1)
    check("failed Anthropic pre-flight returns PRE-FLIGHT FAILED",
          "PRE-FLIGHT FAILED" in (_failed.get("error") or ""), f"result={_failed!r}")
    check("failed Anthropic pre-flight books no estimate", not _bookings, f"bookings={_bookings!r}")
    check("failed Anthropic pre-flight never calls messages.batches.create", not _FakeAnthropic.calls,
          f"calls={_FakeAnthropic.calls!r}")

    submit._preflight_first_request = lambda *args: {"ok": True}
    _FakeAnthropic.calls.clear()
    _passed = submit.submit_message_batch(["hi"], model="claude-opus-4-8", submit=True,
                                          cap_dollars=1000.0, expected_out_tokens=1)
    check("passing Anthropic pre-flight proceeds to messages.batches.create",
          any(name == "batches.create" for name, _ in _FakeAnthropic.calls),
          f"calls={_FakeAnthropic.calls!r}")
    check("submit_message_batch returns the created Anthropic batch id",
          _passed.get("batch_id") == "batch-anthropic-test", f"result={_passed!r}")
finally:
    submit._preflight_first_request = _orig_preflight
    openai.OpenAI = _orig_openai
    anthropic.Anthropic = _orig_anthropic
    gate.record_accepted_batch_estimate = _orig_record
    submit.estimate_jsonl_cost = _orig_estimate_jsonl

print("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails))
sys.exit(1 if _fails else 0)
