"""Offline shape correction preserves the full evidence and the pinned vendor."""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-json-reask-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_recovery, resource_state, guard  # noqa: E402

SCHEMA = {"type": "object", "properties": {"issues": {"type": "array"}}}
PROMPT = "complete evidence\n" * 10000


def exercise_json_lane_reask(reask_limit, corrected_reply):
    resource_state._reset()
    invocations = []
    decisions = []

    def capture_json_lane_invocation(prompt, **kwargs):
        invocations.append((prompt, kwargs))
        return {"text": "not JSON" if len(invocations) == 1 else corrected_reply}

    def isolated_json_recovery_setting(key, default):
        return {"lane_json_reask": reask_limit, "attempts": 1,
                "queue_deadline_s": 0.0, "backoff_s": 0.0}.get(key, default)

    lane = SimpleNamespace(TIMEOUT_S=300, run_prompt=capture_json_lane_invocation)
    with patch.object(adapters, "_lane_for", return_value=("json-test", lane)), \
         patch.object(adapters.config, "api_key", return_value=None), \
         patch.object(lane_recovery, "recovery_setting", side_effect=isolated_json_recovery_setting), \
         patch.object(lane_recovery.LaneRecoveryProof, "authorize_metered_recovery"), \
         patch.object(lane_recovery.LaneRecoveryProof, "assert_metered_recovery"), \
         patch.object(guard, "record_decision", side_effect=lambda decisions=decisions, **kw: decisions.append(kw)):
        result = adapters._call_once("zai:glm-5.2", PROMPT, system="whole system", schema=SCHEMA,
                                     max_tokens=300, _no_sub=True)
    assert all(p == PROMPT for p, _ in invocations)
    if reask_limit:
        assert len(invocations) == 2
        assert "Return ONLY a single valid JSON value matching the schema; no prose, no code fence." in invocations[1][1]["system"]
        assert invocations[0][1]["system"] in invocations[1][1]["system"]
        assert any(e["rung"] == "json_reask" for e in result["lane_recovery"])
    else:
        assert len(invocations) == 1
    if reask_limit and corrected_reply == "{}":
        assert result["executor"] == "json-test" and result["cost"] == 0
        assert not result.get("fell_from")
    else:
        assert result["fell_from"] == "json-test"
        assert result["provider"] == "zai" and result.get("error")
        assert any(e["rung"] == "shed" and e["pinned"] for e in result["lane_recovery"])
        assert any(e["rung"] == "coverage_miss" and e["provider"] == "zai"
                   for e in result["lane_recovery"])


exercise_json_lane_reask(1, "{}")
exercise_json_lane_reask(1, "still not JSON")
exercise_json_lane_reask(0, "{}")

# Strict lane skips produce a per-call decision and preserve the requested vendor.
for vendor, model in (("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5")):
    decisions = []
    with patch.object(adapters, "_lane_for", return_value=("unused", SimpleNamespace())), \
         patch.object(adapters.config, "api_key", return_value=None), \
         patch.object(guard, "record_decision", side_effect=lambda decisions=decisions, **kw: decisions.append(kw)):
        result = adapters._call_once(f"{vendor}:{model}", "evidence", max_tokens=300,
                                     schema={"type": "object", "required": ["issues"]})
    assert result["provider"] == vendor
    assert decisions[0]["why"] == "strict-schema→enforce"
    assert decisions[0]["chosen_model"] == decisions[0]["requested_model"]

print("OK: JSON corrective re-ask and strict routing decisions")
