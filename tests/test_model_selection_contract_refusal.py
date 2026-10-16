"""Offline guard for the unnamed-model selection contract."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-model-contract-")
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["OPENAI_API_KEY"] = "test-key-not-real"
    os.execv(sys.executable, [sys.executable, os.path.realpath(__file__)])

# A gated interpreter may preload the editable main checkout from sitecustomize; evict it so this guard exercises
# the worktree named on sys.path above, not that preloaded package.
_worktree_src = os.path.realpath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")) + os.sep
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "spendguard" or _module_name.startswith("spendguard."):
        _module_file = os.path.realpath(getattr(_module, "__file__", "") or "")
        if not _module_file.startswith(_worktree_src):
            del sys.modules[_module_name]

from spendguard import adapters, best_value, gate, model_catalog  # noqa: E402

print(f"module under test: {adapters.__file__}")

failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


check("model-selection refusal is a SpendGateRefused",
      issubclass(gate.ModelSelectionContractRefused, gate.SpendGateRefused))

for reasoning in (None, "best-value"):
    try:
        adapters.call(None, "offline", measurement=True, reasoning=reasoning)
    except gate.ModelSelectionContractRefused as exc:
        message = str(exc).lower()
        check(f"unnamed measurement refuses ({reasoning=})",
              "measured" in message and "named" in message)
    else:
        check(f"unnamed measurement refuses ({reasoning=})", False)

seen = []
original_guarded = adapters._call_guarded
original_select = best_value.select_model_effort


def stub_guarded(model, prompt, **kwargs):
    seen.append(model)
    return {"text": "ok", "model": model, "provider": model_catalog.provider_of(model), "cost": 0.0,
            "in_tok": 1, "out_tok": 1, "latency": 0.0, "finish_reason": "stop", "error": None,
            "executor": "api"}


try:
    adapters._call_guarded = stub_guarded

    for label, kwargs in (
        ("metered-only unnamed call", {"metered_only": True}),
        ("no-substitution unnamed call", {"no_substitution": True}),
    ):
        seen.clear()
        try:
            result = adapters.call(None, "offline", **kwargs)
        except gate.ModelSelectionContractRefused:
            result = {}
        resolved = seen[-1] if seen else None
        record = model_catalog.model_record(resolved)
        check(f"{label} reaches dispatch with a catalog chat id",
              result.get("text") == "ok" and bool(resolved)
              and (record or {}).get("capabilities", {}).get("mode") == "chat")

    best_value.select_model_effort = lambda *args, **kwargs: {
        "model": "openai:test-model", "effort": None, "why": "offline selection"
    }
    seen.clear()
    result = adapters.call(None, "offline", intent="contract-guard", reasoning="best-value")
    check("unpinned labelled best-value uses the router model",
          result.get("text") == "ok" and seen == ["openai:test-model"])
finally:
    adapters._call_guarded = original_guarded
    best_value.select_model_effort = original_select

print(f"\n{'FAIL' if failures else 'OK'} test_model_selection_contract_refusal: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
