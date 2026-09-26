"""#1 capability-aware auto-route, end to end through _call_once: a STRICT schema (required/nonempty) bound for a
prompt-only LANE whose vendor has a strict METER (anthropic/openai) SKIPS the lane and goes to the strict metered path
— reliable by construction, no reactive churn (Ash 2026-09-26: "lane cannot satisfy but a meter can → straight to
meter"). A LENIENT schema stays on the lane (a lane CAN satisfy it). A strict schema on a COMPAT vendor (no strict
meter — zai/kimi/deepseek) also stays on the lane (there is no strict path to route to).

Offline, isolated home: a fake lane records whether it was CALLED; the metered leg fails fast with no key, so we read
whether the LANE was skipped, never a live success (mirrors tests/test_lane_fallback_is_error_aware.py's harness).
"""
import os
import sys
import tempfile
import types

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-caproute-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, resource_state   # noqa: E402

STRICT = {"type": "object", "required": ["issues"],
          "properties": {"issues": {"type": "array", "items": {
              "type": "object", "required": ["line"], "nonempty": ["line"]}}}}
LENIENT = {"type": "object", "properties": {"issues": {"type": "array"}}}   # no required/nonempty → a lane can satisfy


def ck(results, label, cond):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


def _lane_called(model, schema):
    """True iff the LANE executor's run_prompt was invoked for this call (i.e. the lane was NOT skipped)."""
    prov = model.split(":", 1)[0]
    state = {"called": False}

    def run_prompt(prompt, system=None, model=None, timeout=None, **_kw):
        state["called"] = True
        return {"text": '{"issues": []}', "error": None}

    fake = types.SimpleNamespace(TIMEOUT_S=60, run_prompt=run_prompt)
    _real_lane_for, _real_key = adapters._lane_for, adapters.config.api_key
    adapters._lane_for = lambda p: (f"{prov}-lane", fake) if p == prov else None
    adapters.config.api_key = lambda name: None            # metered leg fails fast (no key) — we only read lane-called
    try:
        resource_state._reset()
        adapters._call_once(model, "review this", max_tokens=300, timeout_s=5, schema=schema)
    finally:
        adapters._lane_for, adapters.config.api_key = _real_lane_for, _real_key
    return state["called"]


def main():
    results = []
    ck(results, "STRICT schema on anthropic → lane SKIPPED (routed to the strict meter)",
       _lane_called("anthropic:claude-opus-4-8", STRICT) is False)
    ck(results, "STRICT schema on openai → lane SKIPPED (routed to the strict meter)",
       _lane_called("openai:gpt-5.5", STRICT) is False)
    ck(results, "LENIENT schema on anthropic → lane USED (a lane can satisfy it; bulk $0 untouched)",
       _lane_called("anthropic:claude-opus-4-8", LENIENT) is True)
    ck(results, "LENIENT schema on zai → lane USED (compat $0 comprehension untouched)",
       _lane_called("zai:glm-5.2", LENIENT) is True)
    ck(results, "STRICT schema on zai (compat) → lane SKIPPED (→ json_object meter; CLI prose-wraps, meter can't)",
       _lane_called("zai:glm-5.2", STRICT) is False)

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_capability_auto_route: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
