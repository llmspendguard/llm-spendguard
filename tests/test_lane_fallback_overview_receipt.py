"""A real failed free-lane dispatch remains a named billed subset in the lane spend overview.

Network transport and recovery queue waiting are mocked; lane dispatch, fell_from context,
recording, and the existing ledger rollup are real.
The tiny fallback is deliberately below the former overview display threshold.
"""
import contextlib
import io
import os
from types import SimpleNamespace
from unittest.mock import patch

import spendguard
spendguard.require()
from spendguard import adapters, calls, config, lane_eligibility, lane_queue, lanes, reliability  # noqa: E402

FALLBACK_OVERVIEW_MODEL = "deepseek:deepseek-v4-flash"
FALLBACK_OVERVIEW_LANE = "overview-fixture-lane"
FALLBACK_OVERVIEW_USD = 0.0012
FALLBACK_OVERVIEW_ORDINARY_USD = 0.0314
FALLBACK_OVERVIEW_PROMPT = "offline fallback overview fixture"
FALLBACK_OVERVIEW_OUTPUT_LIMIT = 32
FALLBACK_OVERVIEW_TIMEOUT = 1
FALLBACK_OVERVIEW_PROVIDER, FALLBACK_OVERVIEW_RAW_MODEL = FALLBACK_OVERVIEW_MODEL.split(":", 1)
FALLBACK_OVERVIEW_ORIGINAL_DISPATCH = adapters._call_once


def fallback_overview_dispatch(model, prompt, *args, **kwargs):
    if kwargs.get("_skip_lane"):
        calls.record_call(FALLBACK_OVERVIEW_PROVIDER, FALLBACK_OVERVIEW_RAW_MODEL, "realtime",
                          FALLBACK_OVERVIEW_USD, executor="api", outcome="ok")
        return {"text": "offline fixture answer", "error": None, "cost": FALLBACK_OVERVIEW_USD,
                "executor": "api", "model": FALLBACK_OVERVIEW_RAW_MODEL}
    return FALLBACK_OVERVIEW_ORIGINAL_DISPATCH(model, prompt, *args, **kwargs)


fallback_overview_dead_lane = SimpleNamespace(
    MIN_TIMEOUT_S=FALLBACK_OVERVIEW_TIMEOUT, TIMEOUT_S=FALLBACK_OVERVIEW_TIMEOUT,
    run_prompt=lambda *args, **kwargs: {"error": "offline fixture unavailable"})

with contextlib.ExitStack() as fallback_overview_stubs:
    fallback_overview_stubs.enter_context(patch.dict(os.environ, {"SPENDGUARD_LANE_BACKOFF_S": "0"}))
    fallback_overview_stubs.enter_context(patch.object(adapters, "_lane_for", lambda provider: (
        FALLBACK_OVERVIEW_LANE, fallback_overview_dead_lane)))
    fallback_overview_stubs.enter_context(patch.object(adapters, "_lane_too_big", lambda *args, **kwargs: False))
    fallback_overview_stubs.enter_context(patch.object(adapters, "_lane_model_cooling", lambda *args, **kwargs: False))
    fallback_overview_stubs.enter_context(patch.object(adapters, "_learn_from_fallback", lambda *args, **kwargs: "transient"))
    fallback_overview_stubs.enter_context(patch.object(adapters, "_call_once", fallback_overview_dispatch))
    fallback_overview_stubs.enter_context(patch.object(lane_queue, "hold_free_recovery", lambda *args, **kwargs: {
        "error": "offline fixture recovery exhausted", "_free_hold": {"deadline_exhausted": True}}))
    fallback_overview_result = adapters.call(FALLBACK_OVERVIEW_MODEL, FALLBACK_OVERVIEW_PROMPT,
                                             max_tokens=FALLBACK_OVERVIEW_OUTPUT_LIMIT,
                                             no_substitution=True, _no_guard=True)

assert fallback_overview_result.get("fell_from") == FALLBACK_OVERVIEW_LANE, fallback_overview_result
assert fallback_overview_result.get("executor") == "api", fallback_overview_result
calls.record_call(FALLBACK_OVERVIEW_PROVIDER, FALLBACK_OVERVIEW_RAW_MODEL, "realtime",
                  FALLBACK_OVERVIEW_ORDINARY_USD, executor="api", outcome="ok")
fallback_overview_rows = reliability.lane_fallback_spend(since=config.month_start_utc())
assert fallback_overview_rows == [{"lane": FALLBACK_OVERVIEW_LANE, "n_calls": 1,
                                   "metered_usd": FALLBACK_OVERVIEW_USD}], fallback_overview_rows

with patch.object(lanes, "lanes_status", return_value={"executor": "api", "lanes": []}), \
        patch.object(lane_eligibility, "summary_line", return_value=None), \
        contextlib.redirect_stdout(io.StringIO()) as fallback_overview_stdout:
    lanes.main([])
fallback_overview_output = fallback_overview_stdout.getvalue()
fallback_overview_named_lines = [line for line in fallback_overview_output.splitlines()
                                 if line.startswith("$0→metered fallback")]
assert len(fallback_overview_named_lines) == 1, fallback_overview_output
assert f"${FALLBACK_OVERVIEW_USD:.4f} across 1 calls" in fallback_overview_named_lines[0], fallback_overview_output
assert FALLBACK_OVERVIEW_LANE in fallback_overview_named_lines[0], fallback_overview_output
assert "included in API spend, not added again" in fallback_overview_named_lines[0], fallback_overview_output
assert f"${FALLBACK_OVERVIEW_ORDINARY_USD:.4f}" not in fallback_overview_named_lines[0], fallback_overview_output
print("OK: actual $0→metered fallback is named, counted, and separate from ordinary API spend in the overview")

with patch.object(lanes, "lanes_status", return_value={"executor": "pool", "lanes": [{
        "lane": FALLBACK_OVERVIEW_LANE, "enabled": True, "auth": "missing",
        "cli": None, "activate": "fixture login", "provider": FALLBACK_OVERVIEW_PROVIDER}]}), \
        patch.object(lane_eligibility, "summary_line", return_value=None), \
        contextlib.redirect_stdout(io.StringIO()) as fallback_overview_enabled_stdout:
    lanes.main([])
fallback_overview_enabled_output = fallback_overview_enabled_stdout.getvalue()
assert fallback_overview_named_lines[0] in fallback_overview_enabled_output, fallback_overview_enabled_output
