"""Offline guard: unnamed chat calls default cheaply; only unnamed measurements refuse."""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-model-default-")
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

from spendguard import adapters, best_value, config, gate, model_catalog  # noqa: E402

print(f"module under test: {adapters.__file__}")

failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


seen = []
original_guarded = adapters._call_guarded
original_select = best_value.select_model_effort
original_chat_default = adapters._chat_default_model
original_cfg_get = config._cfg_get


def stub_guarded(model, prompt, **kwargs):
    seen.append(model)
    return {"text": "ok", "model": model, "provider": model_catalog.provider_of(model), "cost": 0.0,
            "in_tok": 1, "out_tok": 1, "latency": 0.0, "finish_reason": "stop", "error": None,
            "executor": "api"}


try:
    adapters._call_guarded = stub_guarded

    for label, kwargs in (
        ("plain unnamed call", {}),
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
        if kwargs.get("metered_only"):
            check("metered-only default has a priced direct-API path", bool(model_catalog.model_price(resolved)))

    try:
        adapters.call(None, "offline", measurement=True)
    except gate.ModelSelectionContractRefused as exc:
        check("unnamed measurement is the sole contract refusal", "unnamed model cannot be measured" in str(exc))
    else:
        check("unnamed measurement is the sole contract refusal", False)

    chosen = next((record.get("metered_id") or record_id
                   for record_id, record in model_catalog.all_records().items()
                   if (record.get("capabilities") or {}).get("mode") == "chat" and record.get("price")))
    best_value.select_model_effort = lambda *args, **kwargs: {
        "model": chosen, "effort": None, "why": "offline selection"
    }
    adapters._chat_default_model = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("chat default must not override labelled best-value")
    )
    seen.clear()
    result = adapters.call(None, "offline", reasoning="best-value", intent="x")
    check("labelled best-value bypasses chat default and selects its model",
          result.get("text") == "ok" and seen == [chosen])

    adapters._chat_default_model = original_chat_default
    catalog_default = adapters._chat_default_model()
    catalog_record = model_catalog.model_record(catalog_default)
    check("chat default is a catalog chat id",
          (catalog_record or {}).get("capabilities", {}).get("mode") == "chat")
    config._cfg_get = lambda section, key, default=None: (
        "provider:configured-chat" if (section, key) == ("advisor", "default_model") else default
    )
    check("advisor.default_model overrides the catalog default",
          adapters._chat_default_model() == "provider:configured-chat")

    config._cfg_get = lambda section, key, default=None: default
    fake_catalog = types.SimpleNamespace(
        embedding_models=lambda provider=None: [],
        all_records=lambda: {
            "expensive": {"metered_id": "provider:expensive", "capabilities": {"mode": "chat"},
                          "price": {"in_": 2.0}},
            "cheap": {"metered_id": "provider:cheap", "capabilities": {"mode": "chat"},
                      "price": {"in_": 0.1}},
            "embed": {"metered_id": "provider:embed", "capabilities": {"mode": "embedding"},
                      "price": {"in_": 0.001}},
        },
    )
    saved_all_records = model_catalog.all_records
    saved_embedding_models = model_catalog.embedding_models
    model_catalog.all_records = fake_catalog.all_records
    model_catalog.embedding_models = fake_catalog.embedding_models
    try:
        check("chat default chooses the cheaper priced chat row",
              adapters._chat_default_model() == "provider:cheap")
        # A broken/empty catalog with no configured default must RAISE (loud) — never a silent hardcoded fallback.
        model_catalog.all_records = lambda: {}
        try:
            adapters._chat_default_model()
        except gate.ModelSelectionContractRefused as exc:
            check("empty catalog + no default raises (no silent hardcoded fallback)",
                  "no priced chat model" in str(exc))
        else:
            check("empty catalog + no default raises (no silent hardcoded fallback)", False)
    finally:
        model_catalog.all_records = saved_all_records
        model_catalog.embedding_models = saved_embedding_models
finally:
    adapters._call_guarded = original_guarded
    adapters._chat_default_model = original_chat_default
    best_value.select_model_effort = original_select
    config._cfg_get = original_cfg_get

print(f"\n{'FAIL' if failures else 'OK'} test_model_none_defaults_to_cheap_chat: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
