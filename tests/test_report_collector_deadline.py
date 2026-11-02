"""The report's concurrent provider collector must abort loudly without joining a wedged worker."""

import faulthandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from spendguard import report


CHILD_MODE_ARGUMENT = "--collector-deadline-child"
OPENAI_SOURCE_NAME = "OpenAI"
ANTHROPIC_SOURCE_NAME = "Anthropic"
HANG_SOURCE_ARGUMENT_INDEX = 2
INJECTED_COLLECTOR_DEADLINE_S = 0.15
CHILD_TRACEBACK_DEADLINE_FACTOR = 10
PARENT_PROCESS_DEADLINE_FACTOR = 20
CHILD_ELAPSED_DEADLINE_FACTOR = 6
EXPECTED_CHILD_RETURN_CODE = 0
TEST_MONTH_START = "2026-10-01"
HEALTHY_OPENAI_RESULT = ({"2026-10-01": 1.0}, 2)
HEALTHY_ANTHROPIC_RESULT = ({"2026-10-01": 3.0}, {"model": 3.0})
HEALTHY_GPU_RESULT = ({"2026-10-01": 4.0}, None)
REAL_PULL_ERROR_MESSAGE = "real provider failure"


def _hang_forever(*_args, **_kwargs):
    threading.Event().wait()


def _healthy_openai(*_args, **_kwargs):
    return HEALTHY_OPENAI_RESULT


def _healthy_anthropic(*_args, **_kwargs):
    return HEALTHY_ANTHROPIC_RESULT


def _healthy_gpu(*_args, **_kwargs):
    return HEALTHY_GPU_RESULT


def _raise_real_pull_error(*_args, **_kwargs):
    raise ValueError(REAL_PULL_ERROR_MESSAGE)


def _run_hanging_child(hung_source_name):
    report.REPORT_SOURCE_FETCH_DEADLINE_S = INJECTED_COLLECTOR_DEADLINE_S
    report.openai_by_day = _hang_forever if hung_source_name == OPENAI_SOURCE_NAME else _healthy_openai
    report.anth.cost_by_day = _hang_forever if hung_source_name == ANTHROPIC_SOURCE_NAME else _healthy_anthropic
    report.gpu_by_day = _healthy_gpu
    faulthandler.dump_traceback_later(
        INJECTED_COLLECTOR_DEADLINE_S * CHILD_TRACEBACK_DEADLINE_FACTOR,
        exit=True,
    )
    started_at = time.monotonic()
    try:
        report._fetch_sources_concurrently(TEST_MONTH_START)
    except report._WatchdogFired as watchdog_error:
        elapsed_s = time.monotonic() - started_at
        assert hung_source_name in str(watchdog_error), str(watchdog_error)
        assert elapsed_s <= INJECTED_COLLECTOR_DEADLINE_S * CHILD_ELAPSED_DEADLINE_FACTOR, elapsed_s
        faulthandler.cancel_dump_traceback_later()
        print(f"{hung_source_name} bounded in {elapsed_s:.3f}s", flush=True)
        # The intentionally wedged executor thread remains alive after non-blocking shutdown, so bypass Python's
        # normal thread join at interpreter teardown. The collector-return assertion above is the behavior at issue.
        os._exit(EXPECTED_CHILD_RETURN_CODE)
    raise AssertionError(f"{hung_source_name} hang did not raise _WatchdogFired")


def _assert_hanging_scenarios_are_self_bounded():
    test_path = Path(__file__).resolve()
    parent_timeout_s = INJECTED_COLLECTOR_DEADLINE_S * PARENT_PROCESS_DEADLINE_FACTOR
    for hung_source_name in (OPENAI_SOURCE_NAME, ANTHROPIC_SOURCE_NAME):
        child_result = subprocess.run(
            [sys.executable, str(test_path), CHILD_MODE_ARGUMENT, hung_source_name],
            capture_output=True,
            text=True,
            timeout=parent_timeout_s,
            check=False,
        )
        assert child_result.returncode == EXPECTED_CHILD_RETURN_CODE, (
            hung_source_name,
            child_result.returncode,
            child_result.stdout,
            child_result.stderr,
        )


def _assert_healthy_contract_and_real_error_propagation():
    original_openai = report.openai_by_day
    original_anthropic = report.anth.cost_by_day
    original_gpu = report.gpu_by_day
    original_deadline_s = report.REPORT_SOURCE_FETCH_DEADLINE_S
    try:
        report.REPORT_SOURCE_FETCH_DEADLINE_S = INJECTED_COLLECTOR_DEADLINE_S
        report.openai_by_day = _healthy_openai
        report.anth.cost_by_day = _healthy_anthropic
        report.gpu_by_day = _healthy_gpu
        expected_result = HEALTHY_OPENAI_RESULT + HEALTHY_ANTHROPIC_RESULT + HEALTHY_GPU_RESULT
        assert report._fetch_sources_concurrently(TEST_MONTH_START) == expected_result

        report.openai_by_day = _raise_real_pull_error
        try:
            report._fetch_sources_concurrently(TEST_MONTH_START)
        except ValueError as pull_error:
            assert str(pull_error) == REAL_PULL_ERROR_MESSAGE
        else:
            raise AssertionError("real provider exception was not propagated")
    finally:
        report.openai_by_day = original_openai
        report.anth.cost_by_day = original_anthropic
        report.gpu_by_day = original_gpu
        report.REPORT_SOURCE_FETCH_DEADLINE_S = original_deadline_s


if CHILD_MODE_ARGUMENT in sys.argv:
    _run_hanging_child(sys.argv[HANG_SOURCE_ARGUMENT_INDEX])
else:
    _assert_hanging_scenarios_are_self_bounded()
    _assert_healthy_contract_and_real_error_propagation()
    print("OK test_report_collector_deadline")
