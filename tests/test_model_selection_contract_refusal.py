"""Offline guard for unnamed-model routing: explicit pins refuse by name; labelled best-value delegates."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-model-contract-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, gate

failures = []


def assert_model_contract(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


assert_model_contract("named refusal propagates as SpendGateRefused",
                      issubclass(gate.ModelSelectionContractRefused, gate.SpendGateRefused))
for pin in ({"metered_only": True}, {"no_substitution": True}, {"measurement": True}):
    for reasoning in (None, "best-value"):
        try:
            adapters.call(None, "offline", intent="contract-guard", reasoning=reasoning, **pin)
        except gate.ModelSelectionContractRefused as exc:
            message = str(exc)
            assert_model_contract(f"{next(iter(pin))} refuses before routing ({reasoning=})",
                                  "model" in message and "reasoning='best-value'" in message and "intent" in message)
        else:
            assert_model_contract(f"{next(iter(pin))} refuses before routing ({reasoning=})", False)

from spendguard import best_value
original_guarded = adapters._call_guarded
original_select = best_value.select_model_effort
try:
    best_value.select_model_effort = lambda *args, **kwargs: {"model": "openai:test-model", "effort": None,
                                                               "why": "offline selection"}
    adapters._call_guarded = lambda model, prompt, **kwargs: {"text": "ok", "model": model, "provider": "openai",
                                                               "cost": 0.0, "in_tok": 1, "out_tok": 1, "error": None}
    result = adapters.call(None, "offline", intent="contract-guard", reasoning="best-value")
    assert_model_contract("unpinned labelled best-value does not raise the contract refusal", result.get("text") == "ok")
finally:
    adapters._call_guarded = original_guarded
    best_value.select_model_effort = original_select

print(f"\n{'OK' if not failures else 'FAIL'} test_model_selection_contract_refusal: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
