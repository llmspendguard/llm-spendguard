"""Offline raw-storm chaos: whole contracts survive, elapsed deadlines refuse, poison stays named."""
import concurrent.futures
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import adapters, storm_route, storm_submit
from spendguard.storm_coalescer import StormCoalescer

STORM_GUARD_MODEL = "openai:gpt-5-mini"
STORM_GUARD_INTENT = "test:storm-contract-fault-isolation"
STORM_GUARD_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"], "additionalProperties": False}
STORM_GUARD_DEADLINE_S = 3.0
STORM_GUARD_WAIT_S = 5.0
STORM_GUARD_ADMIT_DELAY_S = 0.2
STORM_GUARD_SHORT_DEADLINE_S = 0.1
STORM_GUARD_RATE = 1000.0
STORM_GUARD_JOBS = ("strict", "deadline", "poison")
STORM_GUARD_FAULT = "injected storm poison failure with complete diagnostic"


def run_storm_contract_fault_isolation_guard():
    storm_guard_captured = {}
    storm_guard_lock = threading.Lock()
    storm_guard_all_queued = threading.Event()
    storm_guard_original_submit = StormCoalescer.submit_request
    storm_guard_original_drain = StormCoalescer._drain_cohort
    storm_guard_arrivals = queue.Queue()


    def queue_storm_contract_cohort(self, req):
        future = storm_guard_original_submit(self, req)
        with storm_guard_lock:
            storm_guard_arrivals.put(req)
            if storm_guard_arrivals.qsize() == len(STORM_GUARD_JOBS):
                storm_guard_all_queued.set()
        return future


    def drain_storm_contract_cohort(self):
        assert storm_guard_all_queued.wait(STORM_GUARD_WAIT_S), "storm cohort arrivals did not complete"
        return storm_guard_original_drain(self)


    def capture_storm_contract_dispatch(model, prompt, **kwargs):
        with storm_guard_lock:
            storm_guard_captured[prompt] = kwargs
        if prompt == STORM_GUARD_JOBS[-1]:
            raise ValueError(STORM_GUARD_FAULT)
        return {"text": '{"ok":true}', "status_code": 200, "provider": "openai", "model": model}


    def raw_storm_contract_job(prompt):
        return adapters.call(STORM_GUARD_MODEL, prompt, intent=STORM_GUARD_INTENT, metered_only=True,
                             schema=STORM_GUARD_SCHEMA if prompt == STORM_GUARD_JOBS[0] else None,
                             timeout_s=STORM_GUARD_DEADLINE_S)


    # Force a shared cohort into overflow, exercising production storm_route -> storm_submit ->
    # adapters -> the dispatch sink. No provider call, model inference or billing is made.
    with patch.object(adapters, "_call_guarded", capture_storm_contract_dispatch), \
            patch("spendguard.vendor_call.served_substitute", return_value=(None, None)), \
            patch.object(storm_route.dispatch, "rate_per_s", return_value=STORM_GUARD_RATE), \
            patch.object(StormCoalescer, "_claim_realtime", return_value=0), \
            patch.object(StormCoalescer, "submit_request", queue_storm_contract_cohort), \
            patch.object(StormCoalescer, "_drain_cohort", drain_storm_contract_cohort):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(STORM_GUARD_JOBS)) as pool:
                storm_guard_results = list(pool.map(raw_storm_contract_job, STORM_GUARD_JOBS))
            assert storm_guard_captured[STORM_GUARD_JOBS[0]]["schema"] == STORM_GUARD_SCHEMA
            storm_guard_remaining = storm_guard_captured[STORM_GUARD_JOBS[1]]["timeout_s"]
            assert storm_guard_remaining is not None and 0 < storm_guard_remaining <= STORM_GUARD_DEADLINE_S
            assert all(result.get("text") and not result.get("error") for result in storm_guard_results[:-1])
            assert STORM_GUARD_FAULT in storm_guard_results[-1]["error"]
            assert "ValueError" in storm_guard_results[-1]["error"]
            assert "Traceback" in storm_guard_results[-1]["error"]
            assert "ids=" in storm_guard_results[-1]["error"]
            assert len(storm_route._REG) == 1
            assert sum(coalescer.cohorts for coalescer in storm_route._REG.values()) == 1
        finally:
            storm_route.reset_registry()

    # A queued job whose deadline has elapsed must never reach the sink, including batch fallback.
    storm_guard_before = dict(storm_guard_captured)
    storm_guard_expired = storm_submit.StormRequestContract(STORM_GUARD_JOBS[1],
                                                           deadline_at=time.monotonic() - STORM_GUARD_DEADLINE_S)
    with patch.object(adapters, "call", capture_storm_contract_dispatch):
        storm_guard_executor = storm_submit.governed_realtime_executor(
            STORM_GUARD_MODEL, None, None, STORM_GUARD_INTENT, None)
        storm_guard_refusal = storm_guard_executor(storm_guard_expired)
    assert storm_guard_refusal["error_type"] == "StormDeadlineExceeded"
    assert storm_guard_captured == storm_guard_before

    # The same executor's realtime leg retains the contract, and admission cannot reset
    # its budget. Inject a stalled admission: the provider sink must never be called.
    def stall_storm_contract_admission(*args, **kwargs):
        time.sleep(STORM_GUARD_ADMIT_DELAY_S)
        return SimpleNamespace(ok=True, shed=False, release=lambda: None)


    with patch.object(adapters, "_call_guarded", capture_storm_contract_dispatch), \
            patch("spendguard.vendor_call.served_substitute", return_value=(None, None)), \
            patch.object(storm_route.dispatch, "admit", stall_storm_contract_admission):
        storm_guard_admission_contract = storm_submit.StormRequestContract(
            STORM_GUARD_JOBS[0], schema=STORM_GUARD_SCHEMA,
            deadline_at=time.monotonic() + STORM_GUARD_SHORT_DEADLINE_S)
        storm_guard_admission_refusal = storm_guard_executor(storm_guard_admission_contract)
    assert storm_guard_admission_refusal["error_type"] == "StormDeadlineExceeded"
    assert storm_guard_captured == storm_guard_before
    print("PASS: storm_contract_fault_isolation: whole schema, deadline, poison, expired refusal")


run_storm_contract_fault_isolation_guard()
