"""LiteLLM-sourced capability sync — the GROUND-TRUTH replacement for guessing vision capability (an LLM judge
can't flag models past its cutoff; a hand-list drifts). `_updates_for` reads litellm.model_cost per model and
records vision + a capabilities block, and fills the upper bounds ONLY when the catalog has none (never overwriting
a curated value). A model LiteLLM doesn't cover is LEFT ABSENT (never a guessed flag). Offline (litellm lookup
monkeypatched), zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-synccaps-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import sync_capabilities as sc

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# A controlled "LiteLLM" dataset — a vision chat model, a text-only model, an embedding model, and (implicitly) an
# uncovered one. We PIN _litellm_record so the test proves the mapping logic, not the installed litellm's contents.
_LL = {
    "vmodel": {"supports_vision": True, "supports_response_schema": True, "supports_function_calling": True,
               "mode": "chat", "max_input_tokens": 200000, "max_output_tokens": 64000},
    "tmodel": {"supports_vision": False, "mode": "chat", "max_input_tokens": 32000, "max_output_tokens": 8000},
    "emodel": {"mode": "embedding", "max_input_tokens": 8191},
}
sc._litellm_record = lambda mid, metered: _LL.get(mid) or _LL.get(metered)

# catalog fixtures: vmodel has NO curated limits (should be filled); tmodel HAS a curated ceiling (must be kept);
# emodel is an embedding; umodel is not in _LL at all (uncovered → no update, never a guess).
models = {
    "vmodel": {"metered_id": "vmodel"},
    "tmodel": {"metered_id": "tmodel", "output_ceiling": {"value": 4096, "source": "curated-verified"}},
    "emodel": {"metered_id": "emodel"},
    "umodel": {"metered_id": "umodel"},
}
updates, summary = sc._updates_for(models)

ck("vision True recorded from supports_vision", updates.get("vmodel", {}).get("vision") is True)
ck("vision False recorded from supports_vision", updates.get("tmodel", {}).get("vision") is False)
ck("capabilities block carries response_schema + function_calling + mode",
   updates.get("vmodel", {}).get("capabilities", {}).get("response_schema") is True
   and updates["vmodel"]["capabilities"].get("function_calling") is True
   and updates["vmodel"]["capabilities"].get("mode") == "chat")
ck("MISSING limits are filled from LiteLLM (context_window + output_ceiling, source=litellm)",
   updates["vmodel"].get("context_window") == {"value": 200000, "source": "litellm"}
   and updates["vmodel"].get("output_ceiling") == {"value": 64000, "source": "litellm"})
ck("a CURATED limit is NEVER overwritten (tmodel keeps its verified ceiling)",
   "output_ceiling" not in updates.get("tmodel", {}))
ck("embedding model → mode captured, no vision claimed", updates.get("emodel", {}).get("capabilities", {}).get("mode") == "embedding"
   and "vision" not in updates.get("emodel", {}))
ck("a model LiteLLM does NOT cover gets NO update (never a guessed flag)", "umodel" not in updates)
ck("summary counts: 3 covered, 1 uncovered, vision 1t/1f", summary["covered"] == 3 and summary["uncovered"] == 1
   and summary["vision_true"] == 1 and summary["vision_false"] == 1)

print(("[OK]" if not fails else "[FAIL]") + " sync capabilities: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
