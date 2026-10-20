"""Offline guard: auto replay dispatches the stored strict contract without shortening it."""
import json
from unittest.mock import patch

from spendguard import adapters, bakeoff, callio

CONTRACT_INTENT = "test:stored-contract-replay"
CONTRACT_MODEL = "openai:gpt-5.5"
CONTRACT_PROMPT = "Return the specified object."
CONTRACT_SYSTEM = "Use the strict output contract."
CONTRACT_CEILING = 3072
CONTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["result"],
                   "properties": {"result": {"type": "string", "description": CONTRACT_SYSTEM}}}
CONTRACT_EFFORT = "high"
CONTRACT_SAMPLE_COUNT = 1
CONTRACT_OUTPUT = json.dumps({"result": "complete"})
CONTRACT_SINK = []


def capture_stored_contract_dispatch(model, prompt, **kwargs):
    """Capture an offline candidate call; production dispatch never reaches a provider."""
    CONTRACT_SINK.append((model, prompt, kwargs))
    return {"text": CONTRACT_OUTPUT, "model": model.split(":", 1)[-1], "provider": "openai",
            "cost": 0, "in_tok": 0, "out_tok": 0, "executor": "api", "chosen_effort": kwargs.get("reasoning")}


callio.record_io_sample(CONTRACT_INTENT, "openai", CONTRACT_MODEL, CONTRACT_INTENT, CONTRACT_INTENT,
                        CONTRACT_PROMPT, CONTRACT_OUTPUT, system=CONTRACT_SYSTEM, req_schema=CONTRACT_SCHEMA,
                        req_max_tokens=CONTRACT_CEILING, effort=CONTRACT_EFFORT)
with patch.object(adapters, "call", capture_stored_contract_dispatch), \
        patch.object(bakeoff, "_judge_one", return_value=True):
    contract_result = bakeoff.bakeoff(CONTRACT_INTENT, candidates=[CONTRACT_MODEL],
                                     sample_n=CONTRACT_SAMPLE_COUNT, run=True)
assert contract_result["per_candidate"][CONTRACT_MODEL]["runs"] == CONTRACT_SAMPLE_COUNT, contract_result
assert len(CONTRACT_SINK) == CONTRACT_SAMPLE_COUNT, CONTRACT_SINK
contract_model, contract_prompt, contract_kwargs = CONTRACT_SINK[0]
assert contract_model == CONTRACT_MODEL
assert contract_prompt == CONTRACT_PROMPT
assert contract_kwargs.get("system") == CONTRACT_SYSTEM, contract_kwargs
assert contract_kwargs.get("schema") == CONTRACT_SCHEMA, contract_kwargs
assert contract_kwargs.get("max_tokens") == CONTRACT_CEILING, contract_kwargs
assert contract_kwargs.get("reasoning") == CONTRACT_EFFORT, contract_kwargs
contract_samples = bakeoff._sample_prompts(CONTRACT_INTENT, CONTRACT_SAMPLE_COUNT, include_shape=True)
assert bakeoff._task_sample_key(contract_samples[0]) != bakeoff._task_sample_key(CONTRACT_PROMPT)
print("OK stored strict contract reaches the offline bakeoff dispatch sink")

# Batch history stores the provider envelope rather than the bare JSON Schema.
CONTRACT_ENVELOPE = {"type": "json_schema", "json_schema": {"name": "result", "strict": True,
                                                           "schema": CONTRACT_SCHEMA}}
callio._callio_db().execute("UPDATE call_io SET req_schema=? WHERE intent=?",
                            (json.dumps(CONTRACT_ENVELOPE), CONTRACT_INTENT))
callio._callio_db().commit()
CONTRACT_SINK.clear()
with patch.object(adapters, "call", capture_stored_contract_dispatch), \
        patch.object(bakeoff, "_judge_one", return_value=True):
    bakeoff.bakeoff(CONTRACT_INTENT, candidates=[CONTRACT_MODEL], sample_n=CONTRACT_SAMPLE_COUNT, run=True)
assert CONTRACT_SINK[0][2].get("schema") == CONTRACT_SCHEMA, CONTRACT_SINK
print("OK recovered provider response_format replays the original JSON Schema")
