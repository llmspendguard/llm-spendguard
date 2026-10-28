"""Zero-spend concurrent fault injection through the real adapters recovery path.

No live fidelity claim: fake transport, real registry, durable queue and guardrail-D door.
"""
import atexit
import concurrent.futures
import os
import tempfile
import threading
from unittest.mock import patch

isolated_home_owner = tempfile.TemporaryDirectory(prefix="sg-lane-chaos-")
atexit.register(isolated_home_owner.cleanup)
os.environ["SPENDGUARD_HOME"] = isolated_home_owner.name
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_LANE_ATTEMPTS"] = "3"
os.environ["SPENDGUARD_LANE_BACKOFF_S"] = "0.001"
os.environ["SPENDGUARD_LANE_QUEUE_DEADLINE_S"] = "0.12"
os.environ["SPENDGUARD_LANE_QUEUE_POLL_S"] = "0.002"

from spendguard import adapters, calls, gate, lane_balance, lane_queue, lane_recovery, plan_admission  # noqa: E402

CHAOS_RECOVERY_POLICY = {"SPENDGUARD_AVOID_CLAUDE_LANE": "off"}
CHAOS_RECOVERY_AUTH_STATUS = {"authed": True}


class RecoveringFaultLane:
    TIMEOUT_S = 30
    MIN_TIMEOUT_S = 1

    def __init__(self, fail_count, shape="timeout"):
        self.fail_count = fail_count
        self.shape = shape
        self.lock = threading.Lock()
        self.counts = {}

    def run_prompt(self, prompt, **kwargs):
        with self.lock:
            n = self.counts.get(prompt, 0) + 1
            self.counts[prompt] = n
        if n <= self.fail_count:
            return {"error": self.shape, "status_code": 429 if self.shape == "HTTP 429" else None}
        return {"text": "recovered", "in_tok": 2, "out_tok": 1}


def run_lane_chaos_contract():
    mutation = os.getenv("SPENDGUARD_CHAOS_MUTATION", "intact")
    if mutation == "disable_retry":
        lane_recovery.retry_free_lane = lambda invoke, proof: invoke()
    if mutation == "disable_queue":
        lane_queue.hold_free_recovery = lambda *a, **kw: {"error": "queue disabled"}
    if mutation == "allow_ungated_metered":
        lane_recovery.LaneRecoveryProof.authorize_metered_recovery = lambda *a, **kw: None

    primary_model = "openai:gpt-5.5"
    alternate_model = "anthropic:claude-opus-4-8"
    gate_decisions = []
    metered_sends = []
    real_door = gate._rt_precheck
    real_once = adapters._call_once

    def observe_recovery_door(*args):
        real_door(*args)
        gate_decisions.append(args)

    def simulate_gated_metered_send(model, prompt, **kwargs):
        if kwargs.get("_skip_lane"):
            proof = kwargs.get("_recovery_proof")
            assert proof is not None, "metered send without recovery proof"
            proof.assert_metered_recovery()
            metered_sends.append(prompt)
            calls.record_call("openai", "gpt-5.5", "realtime", 0.0, outcome="ok", disposition="served")
            return {"text": "simulated metered reply", "executor": "api", "cost": 0.0, "error": None}
        return real_once(model, prompt, **kwargs)

    def exercise_fault_wave(fail_count, alternate, shape):
        primary = RecoveringFaultLane(fail_count, shape)
        secondary = RecoveringFaultLane(0)
        mapping = {"openai": ("codex", primary), "anthropic": ("claude-code", secondary)}

        def invoke_fault_caller(n):
            with calls.context(intent="chaos:lane-recovery"):
                return adapters._call_once(primary_model, f"fault-{n}", max_tokens=32)

        substitutes = [alternate_model] if alternate and mutation != "disable_shed" else []
        # The fake alternate must be eligible to prove shedding. Quota-policy acceptance tests separately
        # prove default-on Anthropic refusal; scope this fixture to the mocked wave rather than leaking state.
        with patch.dict(os.environ, CHAOS_RECOVERY_POLICY), \
                patch.object(adapters, "_lane_for", side_effect=lambda p: mapping.get(p)), \
                patch.object(adapters, "_authoritative_lane_auth_status", return_value=CHAOS_RECOVERY_AUTH_STATUS), \
                patch.object(plan_admission, "_ready", side_effect=lambda lane: lane == mapping["anthropic"][0]), \
                patch.object(adapters, "_call_once", side_effect=simulate_gated_metered_send), \
                patch.object(plan_admission, "_best_ready_alt_lane", return_value=None), \
                patch.object(adapters, "_lane_too_big", return_value=False), \
                patch.object(adapters, "_lane_model_cooling", return_value=False), \
                patch.object(lane_balance, "quality_equivalent_free_substitutes", return_value=substitutes), \
                patch.object(adapters, "call", side_effect=lambda m, p, **kw: adapters._call_once(
                    m, p, max_tokens=32, no_metered_fallback=True)), \
                patch.object(adapters.config, "api_key", return_value=None), \
                patch.object(gate, "_rt_precheck", side_effect=observe_recovery_door):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(invoke_fault_caller, range(12)))
        return results, primary

    for shape in ("timeout", "HTTP 429", "not logged in", "cold daemon"):
        results, primary = exercise_fault_wave(2, False, shape)
        assert all(r.get("executor") == "codex" and not r.get("error") for r in results), results
        assert all(n == 3 for n in primary.counts.values()), primary.counts
        for result in results:
            events = result["lane_recovery"]
            assert any(e.get("classification") == "transient" for e in events)
            assert len([e for e in events if e["rung"] == "retry" and e["backoff_s"] > 0]) == 2
    assert not gate_decisions, "recoverable retry reached paid door"

    results, _ = exercise_fault_wave(999, True, "timeout")
    assert all(r.get("executor") == "claude-code" and not r.get("error") for r in results), results
    assert all(any(e["rung"] == "shed" for e in r["lane_recovery"]) for r in results)
    assert not gate_decisions, "ready alternate reached paid door"

    results, primary = exercise_fault_wave(5, False, "HTTP 429")
    assert all(r.get("executor") == "codex" and not r.get("error") for r in results), results
    assert all(n == 6 for n in primary.counts.values())
    assert all(any(e["rung"] == "queue" for e in r["lane_recovery"]) for r in results)
    assert not gate_decisions, "recoverable queued lane reached paid door"
    assert not metered_sends, "a recoverable lane triggered a metered send"
    depth = lane_queue.queue_depth()
    assert depth.get("done", 0) >= 12, depth

    results, _ = exercise_fault_wave(999, False, "timeout")
    assert len(gate_decisions) == 12
    assert len(metered_sends) == 12
    assert all(r.get("executor") == "api" and not r.get("error") for r in results)
    assert all(any(e["rung"] == "metered_authorized" for e in r["lane_recovery"]) for r in results)
    assert all(r.get("cost") in (None, 0) for r in results), "simulation spent money"
    assert lane_queue.queue_depth().get("pending", 0) == 0, "expired holds must not be re-bought by a later drain"

    # An actual normal-door refusal (including a capped-plan operator's refuse-metered policy)
    # must propagate from the concurrent workers before any metered send.
    with patch.dict(os.environ, {"SPENDGUARD_REFUSE_METERED": "1"}):
        try:
            exercise_fault_wave(999, False, "timeout")
        except gate.MeteredCallRefused:
            pass
        else:
            raise AssertionError("normal gate refusal did not halt paid recovery")
    assert len(metered_sends) == 12, "gate refusal reached metered transport"

    # Broken evidence is a typed, recorded deliberate stop even if the provider would accept a send.
    proof = lane_recovery.LaneRecoveryProof("codex")
    try:
        proof.authorize_metered_recovery("openai", "gpt-5.5", "x", None, 32)
    except lane_recovery.LaneRecoveryViolation:
        assert proof.events[-1]["rung"] == "bug"
    else:
        raise AssertionError("missing ladder proof allowed metered")
    print("GREEN lane reliability: concurrent retry, shed, queue, gated last resort, durable outcomes; $0 simulation")


if __name__ == "__main__":
    run_lane_chaos_contract()
