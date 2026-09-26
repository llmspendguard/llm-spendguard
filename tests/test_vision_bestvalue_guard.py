"""best-value must never SWAP a vision (images=) call to a model that can't see the image. The cross-model pick
(best_value.select_model_effort → advisor.recommend_models) is ranked on measured $/good and is NOT told images are
present, so on a vision intent whose measured pool ever included a text-only model it could pick a blind one. The
guard in adapters.call allows a best-value swap on an images= call ONLY to a model model_catalog.vision_capable()
confirms True; otherwise it keeps the caller's own (already-vision) model. A NON-vision call is unaffected.

Data-INDEPENDENT: the catalog's `vision` flags are populated agentically (seed_vision_capability.py --run), so this
test controls capability by monkeypatching, proving the MECHANISM regardless of which models are currently flagged.
Offline (dispatch + image-load + capability monkeypatched), zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-visionbv-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, best_value, model_catalog

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── 1. the accessor: True/False from the record's curated `vision` bool, None when absent/no-record (never assumed) ──
_orig_record = model_catalog.model_record
model_catalog.model_record = lambda mid: (
    {"vision": True} if mid == "vmodel" else
    {"vision": False} if mid == "tmodel" else
    {} if mid == "umodel" else None)                      # umodel = a record with no vision field; else = no record
ck("vision_capable reads True from the record", model_catalog.vision_capable("vmodel") is True)
ck("vision_capable reads False from the record", model_catalog.vision_capable("tmodel") is False)
ck("no `vision` field → None (unknown, never assumed)", model_catalog.vision_capable("umodel") is None)
ck("no record at all → None", model_catalog.vision_capable("absent-xyz") is None)
model_catalog.model_record = _orig_record

# ── 2. the guard in adapters.call — control capability + the best-value pick, capture the dispatched model ──
_captured = {}


def _fake_guarded(model, prompt, **kw):
    _captured["model"] = model                            # the model the dispatch WOULD run — after the guard decided
    return {"text": "ok", "cost": 0.0, "executor": "api", "error": None, "model": model}


adapters._call_guarded = _fake_guarded
adapters._load_image = lambda i: {"data_uri": "data:image/png;base64,AAAA", "tok": 10}
adapters._route_through_queue_enabled = lambda: False
adapters._book_substitution = lambda r: None
# capability is agentic data in prod; here we PIN it so the test proves the guard, not the catalog contents.
model_catalog.vision_capable = lambda m: True if m == "openai:gpt-vision-x" else (False if m == "zai:glm-text-x" else None)


def _pick_model(cand):
    return lambda intent, requested_model, pin_model=False, quality_target=None, prompt=None: {
        "model": cand, "effort": None, "why": "test-pick %s" % cand, "considered": {}}


CALLER = "anthropic:claude-opus-4-8"           # the caller's own (vision) model
IMG = ["data:image/png;base64,AAAA"]

# A) vision call + a NON-vision candidate → BLOCKED: the caller's model is kept (never a blind swap)
best_value.select_model_effort = _pick_model("zai:glm-text-x")
_captured.clear()
adapters.call(CALLER, "describe this image", images=IMG, reasoning="best-value", intent="vision-guard-test")
ck("vision call + non-vision candidate → swap BLOCKED, caller's model kept", _captured.get("model") == CALLER)

# B) vision call + a vision-capable candidate → ALLOWED: economical vision routing preserved
best_value.select_model_effort = _pick_model("openai:gpt-vision-x")
_captured.clear()
adapters.call(CALLER, "describe this image", images=IMG, reasoning="best-value", intent="vision-guard-test")
ck("vision call + vision-capable candidate → swap ALLOWED", _captured.get("model") == "openai:gpt-vision-x")

# C) a vision call + an UNKNOWN-capability candidate → BLOCKED (unknown is not a confirmed yes → keep caller's)
best_value.select_model_effort = _pick_model("mystery:new-model")
_captured.clear()
adapters.call(CALLER, "describe this image", images=IMG, reasoning="best-value", intent="vision-guard-test")
ck("vision call + UNKNOWN candidate → swap BLOCKED (only a confirmed-vision model is swapped to)", _captured.get("model") == CALLER)

# D) a TEXT call (no images) + the non-vision candidate → ALLOWED: the guard applies to vision only
best_value.select_model_effort = _pick_model("zai:glm-text-x")
_captured.clear()
adapters.call(CALLER, "summarize this text", reasoning="best-value", intent="text-guard-test")
ck("text call → guard does NOT apply, candidate swap allowed", _captured.get("model") == "zai:glm-text-x")

print(("[OK]" if not fails else "[FAIL]") + " vision best-value guard: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
