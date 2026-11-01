"""Self-bounded regression guard for bulk_delegate's fan and hedge coordinator deadlines."""
import json
import os
import subprocess
import sys
import tempfile
import time


CHILD_MODE_ENV = "SPENDGUARD_FAN_DEADLINE_CHILD"
INJECTED_DEADLINE_S = 0.04
HEDGE_WINDOW_MS = 1
FAN_TASK_COUNT = 3
FAN_WORKER_COUNT = FAN_TASK_COUNT
HEDGE_TASK_COUNT = 1
HEDGE_WORKER_COUNT = HEDGE_TASK_COUNT
SCENARIO_COUNT = 3
COORDINATOR_RETURN_MULTIPLIER = 8.0
TRACEBACK_WATCHDOG_MULTIPLIER = 2.0
SUBPROCESS_WATCHDOG_MULTIPLIER = 2.0
EXPECTED_RETURN_CODE = 0
FAILURE_RETURN_CODE = 1
JSON_INDENT = 2


def _src_root():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _load_worktree_spendguard():
    # A gated interpreter's startup hook may have imported its installed spendguard before this script starts.
    # Remove that cached package so the guard always exercises this worktree's source, while retaining the SDK
    # monkeypatches the gate already installed in the interpreter.
    for module_name in tuple(sys.modules):
        if module_name == "spendguard" or module_name.startswith("spendguard."):
            del sys.modules[module_name]
    sys.path.insert(0, _src_root())
    from spendguard import adapters, dispatch, lane_balance, lane_bandit, lane_catalog, lane_economics
    return adapters, dispatch, lane_balance, lane_bandit, lane_catalog, lane_economics


def _run_child():
    import faulthandler
    import threading

    adapters, dispatch, lane_balance, lane_bandit, lane_catalog, lane_economics = _load_worktree_spendguard()

    fan_bound_s = lane_balance._bulk_fan_coordinator_bound_s(
        INJECTED_DEADLINE_S, FAN_TASK_COUNT, FAN_WORKER_COUNT)
    hedge_bound_s = (
        lane_balance._bulk_task_coordinator_bound_s(INJECTED_DEADLINE_S)
        + HEDGE_WINDOW_MS / 1000.0
    )
    scenario_bound_s = max(fan_bound_s, hedge_bound_s) * COORDINATOR_RETURN_MULTIPLIER
    child_watchdog_s = scenario_bound_s * SCENARIO_COUNT * TRACEBACK_WATCHDOG_MULTIPLIER
    faulthandler.dump_traceback_later(child_watchdog_s, exit=True)

    arms = [("primary-lane", "primary-model"), ("hedge-lane", "hedge-model")]
    lane_catalog.arms = lambda flt=None: list(arms)
    lane_catalog.lane_provider = lambda lane: "primary-provider" if lane == arms[0][0] else "hedge-provider"
    lane_bandit._arm_cooling = lambda lane, use_name: False
    lane_bandit.arm_stats = lambda intent: {arm: {"winrate": 1.0, "trials": 2} for arm in arms}
    adapters._lane_cooling = lambda lane: False
    lane_economics.prompt_lane_reserved = lambda lane: False
    dispatch.acquire_or_none = lambda *args, **kwargs: 0.0
    dispatch.release = lambda *args, **kwargs: None

    never_returns = threading.Event()
    hung_prompts = set()

    def injected_adapter_call(model, prompt, **kwargs):
        if prompt in hung_prompts:
            never_returns.wait()
        return {"text": "served:" + str(prompt), "cost": 0.0, "model": model.split(":", 1)[-1],
                "provider": model.split(":", 1)[0], "executor": "lane"}

    adapters.call = injected_adapter_call
    report = {}

    dispatch.lane_free = lambda lane: 10 if lane == arms[0][0] else 5
    hung_prompts.clear()
    hung_prompts.add("fan-hung")
    fan_started = time.monotonic()
    fan_rows = lane_balance.bulk_delegate(
        ["fan-hung", "fan-ok-a", "fan-ok-b"], "fan-deadline-guard", max_workers=FAN_WORKER_COUNT,
        chunk_size=FAN_TASK_COUNT, deadline_s=INJECTED_DEADLINE_S)
    report["fan_wall_s"] = time.monotonic() - fan_started
    assert report["fan_wall_s"] <= scenario_bound_s
    assert fan_rows[0]["reason"] == "fan_coordinator_timeout"
    assert fan_rows[0]["text"] is None and fan_rows[0]["billed"] is False
    assert [row["text"] for row in fan_rows[1:]] == ["served:fan-ok-a", "served:fan-ok-b"]

    dispatch.lane_free = lambda lane: 10 if lane == arms[0][0] else 0
    hung_prompts.clear()
    hung_prompts.add("hedge-no-spare")
    no_spare_started = time.monotonic()
    no_spare_rows = lane_balance.bulk_delegate(
        ["hedge-no-spare"], "hedge-no-spare-guard", max_workers=HEDGE_WORKER_COUNT,
        deadline_s=INJECTED_DEADLINE_S, hedge_ms=HEDGE_WINDOW_MS)
    report["hedge_no_spare_wall_s"] = time.monotonic() - no_spare_started
    assert report["hedge_no_spare_wall_s"] <= scenario_bound_s
    assert no_spare_rows[0]["reason"] == "hedge_coordinator_timeout"

    dispatch.lane_free = lambda lane: 10 if lane == arms[0][0] else 5
    hung_prompts.clear()
    hung_prompts.add("hedge-both-hung")
    both_hung_started = time.monotonic()
    both_hung_rows = lane_balance.bulk_delegate(
        ["hedge-both-hung"], "hedge-both-hung-guard", max_workers=HEDGE_WORKER_COUNT,
        deadline_s=INJECTED_DEADLINE_S, hedge_ms=HEDGE_WINDOW_MS)
    report["hedge_both_wall_s"] = time.monotonic() - both_hung_started
    assert report["hedge_both_wall_s"] <= scenario_bound_s
    assert both_hung_rows[0]["reason"] == "hedge_coordinator_timeout"

    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, sort_keys=True, indent=JSON_INDENT), flush=True)
    # Timed-out ThreadPoolExecutor workers are intentionally not joined and cannot be preempted. Exit after flushing
    # assertions so Python's interpreter shutdown cannot turn those deliberately wedged test threads into a hang.
    os._exit(EXPECTED_RETURN_CODE)


def _run_parent():
    _, _, lane_balance, _, _, _ = _load_worktree_spendguard()

    per_scenario_bound_s = lane_balance._bulk_fan_coordinator_bound_s(
        INJECTED_DEADLINE_S, FAN_TASK_COUNT, FAN_WORKER_COUNT) * COORDINATOR_RETURN_MULTIPLIER
    subprocess_watchdog_s = (
        per_scenario_bound_s * SCENARIO_COUNT * TRACEBACK_WATCHDOG_MULTIPLIER
        * SUBPROCESS_WATCHDOG_MULTIPLIER
    )
    child_environment = dict(os.environ)
    child_environment[CHILD_MODE_ENV] = "1"
    child_environment["PYTHONPATH"] = _src_root()
    child_environment["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fan-coordinator-deadline-")
    child_environment["SPENDGUARD_TEST_ISOLATED"] = "1"
    child_environment["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    completed = subprocess.run(
        [sys.executable, os.path.abspath(__file__)], env=child_environment, capture_output=True, text=True,
        timeout=subprocess_watchdog_s)
    if completed.returncode != EXPECTED_RETURN_CODE:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        return FAILURE_RETURN_CODE
    print(completed.stdout.strip())
    print("OK test_lane_balance_fan_coordinator_deadline")
    return EXPECTED_RETURN_CODE


if __name__ == "__main__":
    if os.environ.get(CHILD_MODE_ENV):
        _run_child()
    sys.exit(_run_parent())
