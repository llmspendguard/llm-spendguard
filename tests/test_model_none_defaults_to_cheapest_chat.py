"""Regression: model=None resolves from config/catalog before provider routing. Hermetic and offline."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-model-none-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "0"
os.environ["OPENAI_API_KEY"] = "sk-fake-offline-test-key"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config, model_catalog  # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


chat = model_catalog.chat_models()
expected = chat[0][1]

print("-- reported model=None + metered_only path --")
seen = {}
saved_guarded = adapters._call_guarded
adapters._resolve_guard.on = True


def fake_guarded(model, prompt, **kwargs):
    seen["model"] = model
    return {"provider": adapters.provider_for(model), "model": model, "text": "offline",
            "parsed": None, "in_tok": 1, "out_tok": 1, "cost": 0.0, "latency": 0.0,
            "finish_reason": "stop", "executor": "api", "error": None}


adapters._call_guarded = fake_guarded
try:
    result = adapters.call(model=None, prompt="offline", metered_only=True, intent="t", _route=False)
finally:
    adapters._call_guarded = saved_guarded
    adapters._resolve_guard.on = False

ck("call returns a dict and does not raise", isinstance(result, dict))
ck("model resolves before dispatch to the catalog's cheapest chat model", seen.get("model") == expected)
ck("resolved result model is the default", result.get("model") == adapters._chat_default_model() == expected)
ck("default provenance is stamped", result.get("model_defaulted") is True)

print("\n-- _chat_default_model config/catalog SSOT --")
saved_cfg_get = config._cfg_get
config._cfg_get = lambda section, key, default=None: "provider:configured-chat" \
    if (section, key) == ("advisor", "default_model") else saved_cfg_get(section, key, default)
try:
    ck("advisor.default_model override wins", adapters._chat_default_model() == "provider:configured-chat")
finally:
    config._cfg_get = saved_cfg_get

records = model_catalog.all_records()
priced_chat = []
for rid, rec in records.items():
    if (rec.get("capabilities") or {}).get("mode") == "chat" and (rec.get("price") or {}).get("in_") is not None:
        priced_chat.append(((rec["price"]["in_"]), rec.get("metered_id") or rid))
priced_chat.sort(key=lambda row: (row[0], row[1]))
ck("unset override uses minimum catalog chat input price", expected == priced_chat[0][1])
expected_rec = model_catalog.model_record(expected)
ck("default is chat, not embedding/other mode", (expected_rec.get("capabilities") or {}).get("mode") == "chat")
ck("default is not vision-only", (expected_rec.get("capabilities") or {}).get("mode") != "vision")

print("\n-- provider_for guard --")
try:
    adapters.provider_for(None)
    ck("provider_for(None) raises ValueError", False)
except ValueError as exc:
    ck("provider_for(None) raises ValueError, not TypeError", "non-empty" in str(exc) and "None" in str(exc))
except TypeError:
    ck("provider_for(None) raises ValueError, not TypeError", False)
ck("valid default id resolves to its catalog provider",
   adapters.provider_for(expected) == expected_rec.get("provider"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_model_none_defaults_to_cheapest_chat: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
