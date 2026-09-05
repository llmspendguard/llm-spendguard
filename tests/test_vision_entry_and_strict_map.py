"""Vision is a DISCOVERABLE, fail-loud entry; a keyed-map schema is refused, never silently emptied.

Two failures compounded in a real image labeler (7thsense): (1) the vision request was not sent via `images=`, so
it rode a text-only lane and cold-400'd — `no_substitution=True` did NOT help because it does not skip the lane,
only `images` does; (2) the schema was a DYNAMIC-KEY MAP (labels:{<id>:<label>}), which OpenAI strict mode
collapses to {} with no error, so the model 'correctly' returned {"labels": {}}.

This guards both fixes: adapters.vision (an explicit entry that REQUIRES a non-empty image and routes to the API),
and strict_map_violation / json_schema_request refusing a map schema on the OpenAI strict path with a typed
SchemaNotStrictExpressible instead of returning the empty object. Offline: no network, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-vision-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters
from spendguard.adapters import strict_map_violation, json_schema_request, SchemaNotStrictExpressible

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# schemas
MAP = {"type": "object", "required": ["labels"],
       "properties": {"labels": {"type": "object", "additionalProperties": {"type": "string"}}}}
LIST = {"type": "object", "required": ["results"],
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "required": ["id", "label"],
            "properties": {"id": {"type": "string"}, "label": {"type": "string"}}}}}}
CLOSED = {"type": "object", "additionalProperties": False,
          "properties": {"x": {"type": "string"}}, "required": ["x"]}
NESTED_MAP = {"type": "object", "properties": {"results": {"type": "array", "items": {
    "type": "object", "properties": {"tags": {"type": "object", "additionalProperties": {"type": "number"}}}}}}}

# ── strict_map_violation: STRUCTURAL detection of a dynamic-key map ──
print("-- strict_map_violation --")
ck("a dynamic-key map is detected, path named", strict_map_violation(MAP) == "$.labels")
ck("a list-of-objects schema is clean (None)", strict_map_violation(LIST) is None)
ck("additionalProperties:false is NOT a map (clean)", strict_map_violation(CLOSED) is None)
ck("a map nested inside an array item is found", strict_map_violation(NESTED_MAP) == "$.results[].tags")

# ── json_schema_request: refuse the map on OpenAI strict; allow it where it does not collapse ──
print("\n-- json_schema_request (vendor-aware) --")
raised = None
try:
    json_schema_request("openai", MAP)
except SchemaNotStrictExpressible as e:
    raised = e
ck("openai + map → raises SchemaNotStrictExpressible", raised is not None)
ck("...the error names the collapsing path and the fix", raised is not None and raised.path == "$.labels" and "ARRAY" in str(raised))
ck("openai + list schema → no raise (strict-expressible)", isinstance(json_schema_request("openai", LIST), dict))
# anthropic (forced tool, not strict) and compat (json_object + prompt) do NOT collapse a map → not refused here
ck("anthropic + map → NOT refused (tool schema allows maps)", isinstance(json_schema_request("anthropic", MAP), dict))
ck("compat + map → NOT refused (json_object; shape rides the prompt)", isinstance(json_schema_request("compat", MAP), dict))

# ── the collapse the guard PREVENTS: _openai_strict would empty the map to {}-only ──
print("\n-- the silent collapse the guard prevents --")
collapsed = adapters._openai_strict(MAP)["properties"]["labels"]
ck("_openai_strict turns the map into additionalProperties:false + required:[] (only {} valid)",
   collapsed.get("additionalProperties") is False and collapsed.get("required") == [])

# ── adapters.vision: an explicit entry that REFUSES a no-image call and forwards images= ──
print("\n-- adapters.vision entry --")
raised2 = None
try:
    adapters.vision("openai:gpt-5-nano", "label this", [])
except ValueError as e:
    raised2 = e
ck("vision(images=[]) raises (a no-image vision call is a bug, not a text call)", raised2 is not None and "at least one image" in str(raised2))

_seen = {}
_orig = adapters.call
adapters.call = lambda model, prompt, **kw: (_seen.update(kw, model=model), {"text": "ok", "cost": 0.001, "executor": "api"})[1]
try:
    r = adapters.vision("openai:gpt-5-nano", "label", ["/tmp/x.png"], schema=LIST, sig="label")
finally:
    adapters.call = _orig
ck("vision forwards images= to call (non-empty)", _seen.get("images") == ["/tmp/x.png"])
ck("vision forwards the schema and sig through", _seen.get("schema") == LIST and _seen.get("sig") == "label")
ck("vision returns call's result dict", r.get("text") == "ok" and r.get("executor") == "api")

print(("[OK]" if not fails else "[FAIL]") + " vision entry + strict-map guard: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
