"""Shared machine-speed calibration for isolated test runners and their children."""
import math
import os
import time


RUNNER_YARDSTICK_ITERATIONS = 10_000_000
RUNNER_YARDSTICK_MODULUS = 1_000_000_007
# Measured 2026-10-21 on the reference dev machine, gated Python 3.11,
# time.monotonic around runner_budget_cpu_yardstick, without coverage tracing.
RUNNER_YARDSTICK_BASELINE_S = 0.3443317089986522
MIN_RUNNER_SCALE = 1.0
MAX_RUNNER_SCALE = 4.0
RUNNER_BUDGET_SCALE_ENV = "SPENDGUARD_RUNNER_BUDGET_SCALE"


class RunnerBudgetCalibrationError(RuntimeError):
    """Calibration cannot establish a finite positive compute measurement."""


def effective_runner_budget_scale():
    """Return the validated runner budget scale clamped to the supported range."""
    raw_scale = os.environ.get(RUNNER_BUDGET_SCALE_ENV, "1.0")
    try:
        parsed_scale = float(raw_scale)
    except ValueError as error:
        raise RunnerBudgetCalibrationError(f"invalid runner budget scale: {raw_scale!r}") from error
    if not math.isfinite(parsed_scale) or parsed_scale <= 0:
        raise RunnerBudgetCalibrationError(f"invalid runner budget scale: {raw_scale!r}")
    return max(MIN_RUNNER_SCALE, min(MAX_RUNNER_SCALE, parsed_scale))


def runner_budget_cpu_yardstick():
    """Fixed pure integer CPU work: no I/O, randomness, sleeps or dependencies."""
    yardstick_accumulator = 0
    for yardstick_index in range(RUNNER_YARDSTICK_ITERATIONS):
        yardstick_accumulator = (
            yardstick_accumulator + yardstick_index * yardstick_index
        ) % RUNNER_YARDSTICK_MODULUS
    return yardstick_accumulator


def measure_isolated_runner_budget_scale():
    """Measure the bounded machine-speed multiplier on a runner controller."""
    yardstick_started = time.monotonic()
    runner_budget_cpu_yardstick()
    yardstick_elapsed = time.monotonic() - yardstick_started
    if not math.isfinite(yardstick_elapsed) or yardstick_elapsed <= 0:
        raise RunnerBudgetCalibrationError(f"invalid yardstick elapsed: {yardstick_elapsed!r}")
    if not math.isfinite(RUNNER_YARDSTICK_BASELINE_S) or RUNNER_YARDSTICK_BASELINE_S <= 0:
        raise RunnerBudgetCalibrationError("invalid reference yardstick baseline")
    measured_scale = max(
        MIN_RUNNER_SCALE,
        min(MAX_RUNNER_SCALE, yardstick_elapsed / RUNNER_YARDSTICK_BASELINE_S),
    )
    return yardstick_elapsed, measured_scale
