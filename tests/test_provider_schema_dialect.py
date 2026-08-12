"""Our contract schema and a provider's schema are DIFFERENT LANGUAGES that look identical.

WHY THIS GUARD EXISTS. output_contract's schema carries `nonempty` — a spendguard concept, not JSON Schema,
which no provider has heard of. The harness handed that contract straight to the providers as if it were a
schema, and the results looked like vendor failures:

    gpt-5.5   400: "'additionalProperties' is required to be supplied and to be false"   -> reported as
              "openai cannot do strict schemas"
    kimi-k3   $.issues[0]: expected object, got str    -> the compat path said "return JSON" and never said
    glm-5.2   $: missing required key 'issues'            WHICH json, so the model invented a shape

Same class of error as reading a context window as an output ceiling: two things of the same shape meaning
different things. After the fix the `strict` rung went from 1 of 4 vendors passing to 4 of 4 — so three
"vendor failures" were ours.
"""
import json
import sys

from spendguard.adapters import json_schema_request

CONTRACT = {"type": "object", "required": ["issues"],
            "properties": {"issues": {"type": "array",
                                      "items": {"type": "object",
                                                "required": ["line", "issue"],
                                                "nonempty": ["issue"],          # OURS, not JSON Schema
                                                "properties": {"line": {"type": "integer"},
                                                               "issue": {"type": "string"}}}}}}


def test_our_private_keyword_never_reaches_any_provider():
    for kind in ("anthropic", "openai", "compat"):
        blob = json.dumps(json_schema_request(kind, CONTRACT))
        assert "nonempty" not in blob, (
            f"{kind}: `nonempty` is a spendguard concept checked against the RESPONSE. Sending it as if it "
            f"were JSON Schema is what produced a 400 and two invented shapes.")


def test_openai_strict_mode_rules_are_applied_recursively():
    """Strict mode has two rules beyond JSON Schema, and breaking either is a hard 400, not a soft failure:
    every object sets additionalProperties=false, and `required` lists EVERY property."""
    s = json_schema_request("openai", CONTRACT)["response_format"]["json_schema"]["schema"]
    assert s["additionalProperties"] is False
    assert s["required"] == ["issues"]
    item = s["properties"]["issues"]["items"]
    assert item["additionalProperties"] is False, "nested objects must comply too, or the request 400s"
    assert sorted(item["required"]) == ["issue", "line"]


def test_compat_endpoints_are_TOLD_the_shape_not_just_told_to_use_json():
    """json_object guarantees parseable JSON and nothing about the shape. If the shape does not travel in the
    prompt, the model has no way to know it — and it will confidently return a different one."""
    req = json_schema_request("compat", CONTRACT)
    assert req["response_format"] == {"type": "json_object"}
    assert "_schema_prompt" in req, "a compat endpoint's only channel for the shape is the prompt"
    assert "issues" in req["_schema_prompt"], "the actual schema must be in it, not a vague instruction"


def test_the_schema_prompt_is_never_sent_as_an_api_parameter():
    """It is ours. Passing it through to the SDK would be an unknown kwarg and a 400."""
    import inspect
    from spendguard import adapters
    # THE REQUEST BUILDER MOVED. `adapters.call` is now the guarded entry point (input-size check +
    # truncation retry) and `_call_once` builds and sends the request. These checks are about the
    # request, so they read the builder. Reading `call` here would inspect the guard wrapper and
    # pass vacuously — a source-reading test silently detaches from its subject when code moves.
    src = inspect.getsource(adapters._call_once)
    assert '_schema_prompt' in src and 'pop(' in src, \
        "adapters.call must POP _schema_prompt and fold it into the system message"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [OK] {name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {name} — {e}")
    print(f"\n{'[FAIL]' if fails else 'OK'} test_provider_schema_dialect: {fails} failure(s)")
    sys.exit(1 if fails else 0)
