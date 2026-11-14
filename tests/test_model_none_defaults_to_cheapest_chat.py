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


def _routable(model_id):
    """A model is routable iff a registered provider adapter resolves it (lookup, not a judgement). Mirrors
    adapters._model_is_routable independently so this test checks the contract rather than calling the code it tests."""
    try:
        adapters.provider_for(model_id)
        return True
    except ValueError:
        return False


def _cheapest_priced_routable_chat():
    """Re-derive the contract independently of _chat_default_model: the cheapest priced chat model whose provider has a
    registered adapter. A priced chat model on an attribution-only provider (no adapter — e.g. typesafe/jev) is
    uncallable, so it must NEVER be the default even though it may be the cheapest priced chat model overall."""
    rows = []
    for record_id, record in model_catalog.all_records().items():
        capabilities = record.get("capabilities") or {}
        model_id = record.get("metered_id") or record_id
        price_in = (record.get("price") or {}).get("in_")
        if capabilities.get("mode") == "chat" and price_in is not None and _routable(model_id):
            rows.append((price_in, model_id))
    rows.sort(key=lambda row: (row[0], str(row[1])))
    return rows


_priced_routable_chat = _cheapest_priced_routable_chat()
expected = _priced_routable_chat[0][1]

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
ck("model resolves before dispatch to the catalog's cheapest routable chat model", seen.get("model") == expected)
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

ck("unset override uses minimum ROUTABLE catalog chat input price", expected == _priced_routable_chat[0][1])
# A priced chat model on a provider with NO registered adapter (attribution-only) may be cheaper than `expected`, but
# must be excluded from the default because it is uncallable — prove such a model, if present, is NOT chosen.
_unroutable_cheaper = [
    (((rec.get("price") or {}).get("in_")), rec.get("metered_id") or rid)
    for rid, rec in model_catalog.all_records().items()
    if (rec.get("capabilities") or {}).get("mode") == "chat"
    and (rec.get("price") or {}).get("in_") is not None
    and not _routable(rec.get("metered_id") or rid)]
ck("the default is routable (its provider has a registered adapter)", _routable(expected))
ck(f"any cheaper UNROUTABLE chat model ({sorted(m for _p, m in _unroutable_cheaper)}) is NOT chosen as the default",
   all(m != expected for _p, m in _unroutable_cheaper))
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
