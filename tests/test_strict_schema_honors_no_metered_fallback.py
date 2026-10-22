"""Strict-schema lane routing must preserve an explicit no-metered-fallback guarantee.

Offline and isolated: a fake lane records whether it was called, while a fake key lookup records entry into the
metered path and returns no key before any network request can occur.
"""
import os
import sys
import tempfile
import types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-strict-schema-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, guard, resource_state  # noqa: E402


STRICT_SCHEMA = {
    "type": "object",
    "required": ["issues"],
    "properties": {"issues": {"type": "array"}},
}
LENIENT_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array"}},
}
STRICT_VENDOR_MODEL = "anthropic:claude-opus-4-8"
COMPAT_VENDOR_MODEL = "zai:glm-5.2"
STRICT_VENDOR_LANE = "anthropic-lane"
VALID_LANE_REPLY = '{"issues": []}'
CALL_PROMPT = "review this"
CALL_MAX_TOKENS = 300
CALL_TIMEOUT_SECONDS = 5
FAKE_LANE_TIMEOUT_SECONDS = 60
SUPPRESSED_DECISION_WHY = "strict-schema→enforce-suppressed:no_metered_fallback"
ENFORCED_DECISION_WHY = "strict-schema→enforce"


def exercise_strict_schema_route(model, schema, no_metered_fallback):
    provider = model.split(":", 1)[0]
    observed = {"lane_calls": 0, "metered_calls": 0, "decisions": []}

    def run_fake_strict_schema_lane(prompt, system=None, model=None, timeout=None, **kwargs):
        observed["lane_calls"] += 1
        return {"text": VALID_LANE_REPLY, "error": None}

    def reject_strict_schema_metered_key_lookup(name):
        observed["metered_calls"] += 1
        return None

    def record_strict_schema_routing_decision(**decision):
        observed["decisions"].append(decision)

    fake_lane = types.SimpleNamespace(
        TIMEOUT_S=FAKE_LANE_TIMEOUT_SECONDS,
        run_prompt=run_fake_strict_schema_lane,
    )
    real_lane_for = adapters._lane_for
    real_api_key = adapters.config.api_key
    real_record_decision = guard.record_decision
    adapters._lane_for = (
        lambda candidate: (f"{provider}-lane", fake_lane) if candidate == provider else None
    )
    adapters.config.api_key = reject_strict_schema_metered_key_lookup
    guard.record_decision = record_strict_schema_routing_decision
    resource_state._reset()
    try:
        result = adapters._call_once(
            model,
            CALL_PROMPT,
            max_tokens=CALL_MAX_TOKENS,
            timeout_s=CALL_TIMEOUT_SECONDS,
            schema=schema,
            no_metered_fallback=no_metered_fallback,
        )
    finally:
        adapters._lane_for = real_lane_for
        adapters.config.api_key = real_api_key
        guard.record_decision = real_record_decision
    return observed, result


def test_strict_schema_keeps_free_lane_when_metered_fallback_is_forbidden():
    observed, result = exercise_strict_schema_route(
        STRICT_VENDOR_MODEL, STRICT_SCHEMA, no_metered_fallback=True
    )
    return (
        observed["lane_calls"] == 1
        and observed["metered_calls"] == 0
        and result["executor"] == STRICT_VENDOR_LANE
        and [decision["why"] for decision in observed["decisions"]] == [SUPPRESSED_DECISION_WHY]
    )


def test_strict_schema_uses_metered_path_when_fallback_is_permitted():
    observed, _result = exercise_strict_schema_route(
        STRICT_VENDOR_MODEL, STRICT_SCHEMA, no_metered_fallback=False
    )
    return (
        observed["lane_calls"] == 0
        and observed["metered_calls"] == 1
        and [decision["why"] for decision in observed["decisions"]] == [ENFORCED_DECISION_WHY]
    )


def test_non_strict_schema_paths_never_reroute():
    for model, schema in (
        (STRICT_VENDOR_MODEL, LENIENT_SCHEMA),
        (COMPAT_VENDOR_MODEL, STRICT_SCHEMA),
    ):
        observed, _result = exercise_strict_schema_route(
            model, schema, no_metered_fallback=False
        )
        if (
            observed["lane_calls"] != 1
            or observed["metered_calls"] != 0
            or observed["decisions"]
        ):
            return False
    return True


def main():
    checks = (
        ("strict schema + no metered fallback keeps the free lane",
         test_strict_schema_keeps_free_lane_when_metered_fallback_is_forbidden()),
        ("strict schema + metered fallback permitted takes the metered path",
         test_strict_schema_uses_metered_path_when_fallback_is_permitted()),
        ("lenient schemas and compat vendors never reroute",
         test_non_strict_schema_paths_never_reroute()),
    )
    for label, passed in checks:
        print(f"  [{'OK' if passed else 'FAIL'}] {label}")
    failures = sum(not passed for _label, passed in checks)
    print(f"\n{'[FAIL]' if failures else 'OK'} test_strict_schema_honors_no_metered_fallback: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
