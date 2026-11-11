"""Offline guard for the thread-safe, per-call executor override.

Mutation proof, run before the final green verification:
1. Making ``_lane_for`` ignore its ``executor`` argument reddens the two ambient-override assertions.
2. Removing ``executor=executor`` from either ``bulk_delegate`` task runner reddens its forwarding assertions.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard
spendguard.require()

from spendguard import adapters, dispatch, lane_balance, lane_bandit, lane_catalog, lane_economics


EXECUTOR_ENV = "SPENDGUARD_ADVISOR_EXECUTOR"
API_EXECUTOR = "api"
POOL_EXECUTOR = "pool"
TEST_PROVIDER = "executor-test-provider"
TEST_LANE = "executor-test-lane"
TEST_MODULE = "executor_test_lane"
TEST_MODEL = f"{TEST_PROVIDER}:executor-test-model"
TEST_INTENT = "executor-param-scope"
TEST_TASK = "executor-param-task"

failures = []


def check_executor_scope(label, condition):
    if not condition:
        failures.append(label)
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


saved_lanes = adapters._LANES
saved_lane_cooling = adapters._lane_cooling
saved_ambient = os.environ.get(EXECUTOR_ENV)
module_name = f"{adapters.__package__}.{TEST_MODULE}"
fake_lane_module = types.SimpleNamespace()
sys.modules[module_name] = fake_lane_module
adapters._LANES = {TEST_PROVIDER: (TEST_LANE, TEST_MODULE)}
adapters._lane_cooling = lambda lane: False
try:
    os.environ[EXECUTOR_ENV] = API_EXECUTOR
    before_override = os.environ.get(EXECUTOR_ENV)
    pool_result = adapters._lane_for(TEST_PROVIDER, executor=POOL_EXECUTOR)
    check_executor_scope("executor=pool selects the lane under an api ambient",
                         pool_result == (TEST_LANE, fake_lane_module))
    check_executor_scope("the pool override does not mutate os.environ",
                         os.environ.get(EXECUTOR_ENV) == before_override)

    os.environ[EXECUTOR_ENV] = POOL_EXECUTOR
    before_override = os.environ.get(EXECUTOR_ENV)
    check_executor_scope("executor=api selects metered under a pool ambient",
                         adapters._lane_for(TEST_PROVIDER, executor=API_EXECUTOR) is None)
    check_executor_scope("the api override does not mutate os.environ",
                         os.environ.get(EXECUTOR_ENV) == before_override)

    for ambient_executor in (API_EXECUTOR, POOL_EXECUTOR):
        os.environ[EXECUTOR_ENV] = ambient_executor
        check_executor_scope(f"executor=None preserves ambient {ambient_executor} behavior",
                             adapters._lane_for(TEST_PROVIDER, executor=None)
                             == adapters._lane_for(TEST_PROVIDER))

    os.environ[EXECUTOR_ENV] = API_EXECUTOR
    sequence = (adapters._lane_for(TEST_PROVIDER, executor=POOL_EXECUTOR),
                adapters._lane_for(TEST_PROVIDER, executor=API_EXECUTOR))
    check_executor_scope("sequential overrides remain call-scoped with no leakage",
                         sequence[0] == (TEST_LANE, fake_lane_module) and sequence[1] is None
                         and os.environ.get(EXECUTOR_ENV) == API_EXECUTOR)
finally:
    adapters._LANES = saved_lanes
    adapters._lane_cooling = saved_lane_cooling
    sys.modules.pop(module_name, None)
    if saved_ambient is None:
        os.environ.pop(EXECUTOR_ENV, None)
    else:
        os.environ[EXECUTOR_ENV] = saved_ambient


saved_call = adapters.call
saved_acquire = dispatch.acquire_or_none
saved_release = dispatch.release
saved_catalog_arms = lane_catalog.arms
saved_lane_provider = lane_catalog.lane_provider
saved_arm_cooling = lane_bandit._arm_cooling
saved_arm_stats = lane_bandit.arm_stats
saved_lane_reserved = lane_economics.prompt_lane_reserved
seen_executor_scopes = []


def capture_executor_scope(model, prompt, **kwargs):
    seen_executor_scopes.append(kwargs.get("executor"))
    provider, raw_model = model.split(":", 1)
    return {"text": "offline", "cost": None, "provider": provider, "model": raw_model,
            "executor": kwargs.get("executor")}


adapters.call = capture_executor_scope
dispatch.acquire_or_none = lambda *args, **kwargs: object()
dispatch.release = lambda *args, **kwargs: None
lane_catalog.arms = lambda flt=None: [(TEST_LANE, TEST_MODEL.split(":", 1)[1])]
lane_catalog.lane_provider = lambda lane: TEST_PROVIDER
lane_bandit._arm_cooling = lambda lane, model: False
lane_bandit.arm_stats = lambda intent: {}
lane_economics.prompt_lane_reserved = lambda lane: False
try:
    for executor_override in (API_EXECUTOR, POOL_EXECUTOR, None):
        seen_executor_scopes.clear()
        lane_balance.bulk_delegate([TEST_TASK], TEST_INTENT, model_for=lambda task: TEST_MODEL,
                                   executor=executor_override, force=True)
        check_executor_scope(f"api task runner forwards executor={executor_override!r}",
                             seen_executor_scopes == [executor_override])

        seen_executor_scopes.clear()
        lane_balance.bulk_delegate([TEST_TASK], TEST_INTENT, executor=executor_override, force=True)
        check_executor_scope(f"lane task runner forwards executor={executor_override!r}",
                             seen_executor_scopes == [executor_override])
finally:
    adapters.call = saved_call
    dispatch.acquire_or_none = saved_acquire
    dispatch.release = saved_release
    lane_catalog.arms = saved_catalog_arms
    lane_catalog.lane_provider = saved_lane_provider
    lane_bandit._arm_cooling = saved_arm_cooling
    lane_bandit.arm_stats = saved_arm_stats
    lane_economics.prompt_lane_reserved = saved_lane_reserved


print(f"\n{'[FAIL]' if failures else 'OK'} test_executor_param_scopes_per_call: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
