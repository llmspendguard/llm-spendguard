"""LiteLLM-sourced capability sync — the GROUND-TRUTH replacement for guessing vision capability (an LLM judge
can't flag models past its cutoff; a hand-list drifts). Covers: (A) _litellm_record prefers the daily-refreshed
CACHE (capabilities + context merged) over the installed package; (B) _updates_for maps a record → vision +
capabilities + fills only MISSING limits (never overwrites curated); (C) audit_catalog_completeness reports coverage
+ uncovered + discovers provider models not in the catalog. Offline (cache + litellm lookup controlled), zero spend."""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-synccaps-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import sync_capabilities as sc, sync

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── A. _litellm_record prefers the daily-github CACHE, merging its capabilities + context (limits) sections ──
_cache = os.path.join(os.environ["SPENDGUARD_HOME"], "litellm_prices.json")
with open(_cache, "w") as fh:
    json.dump({"capabilities": {"cachehit": {"supports_vision": True, "mode": "chat"}},
               "context": {"cachehit": {"max_input_tokens": 200000, "max_output_tokens": 64000}}}, fh)
sync.CACHE = _cache                                        # point the reader at our controlled cache
rec = sc._litellm_record("cachehit", None)
ck("cache-read MERGES capabilities + context (limits) into one record",
   rec.get("supports_vision") is True and rec.get("max_input_tokens") == 200000 and rec.get("max_output_tokens") == 64000)
ck("a model absent from the cache → None from the cache path (before any package fallback)",
   sc._litellm_record("not-in-cache-xyz", None) is None)

# ── B. _updates_for maps a record → vision + capabilities, fills only MISSING limits ──
_LL = {
    "vmodel": {"supports_vision": True, "supports_response_schema": True, "supports_function_calling": True,
               "mode": "chat", "max_input_tokens": 200000, "max_output_tokens": 64000},
    "tmodel": {"supports_vision": False, "mode": "chat", "max_input_tokens": 32000, "max_output_tokens": 8000},
}
sc._litellm_record = lambda mid, metered: _LL.get(mid) or _LL.get(metered)   # pin for the mapping tests
models = {
    "vmodel": {"metered_id": "vmodel"},
    "tmodel": {"metered_id": "tmodel", "output_ceiling": {"value": 4096, "source": "curated-verified"}},
    "umodel": {"metered_id": "umodel"},                    # not in _LL → uncovered
}
updates, summary = sc._updates_for(models)
ck("vision True + capabilities block recorded",
   updates.get("vmodel", {}).get("vision") is True
   and updates["vmodel"].get("capabilities", {}).get("response_schema") is True)
ck("MISSING limits filled from LiteLLM (source=litellm)",
   updates["vmodel"].get("output_ceiling") == {"value": 64000, "source": "litellm"})
ck("a CURATED limit is NEVER overwritten", "output_ceiling" not in updates.get("tmodel", {}))
ck("uncovered model → no update (never a guessed flag)", "umodel" not in updates)
ck("summary counts", summary["covered"] == 2 and summary["uncovered"] == 1
   and summary["vision_true"] == 1 and summary["vision_false"] == 1)

# ── C. audit_catalog_completeness — coverage, uncovered, and DISCOVER (provider models not in the catalog) ──
from spendguard import model_catalog as mc
mc.all_records = lambda: {
    "vmodel": {"provider": "openai", "metered_id": "vmodel"},           # covered (in _LL)
    "custommodel": {"provider": "openai", "metered_id": "custommodel"},  # uncovered (not in _LL), no vision
}
# a controlled cache providers section: openai has a NEWER model our catalog lacks; a provider we DON'T use is ignored
with open(_cache, "w") as fh:
    json.dump({"capabilities": {}, "context": {},
               "providers": {"vmodel": "openai", "gpt-6-new": "openai", "some-cohere": "cohere"}}, fh)
rep = sc.audit_catalog_completeness()
ck("coverage counts covered vs uncovered", rep["catalog_n"] == 2 and rep["covered"] == 1 and rep["uncovered"] == ["custommodel"])
ck("no_vision lists models with no vision verdict", "custommodel" in rep["no_vision"])
ck("DISCOVER surfaces a provider model not in the catalog (gpt-6-new)", rep["discover"].get("openai") == ["gpt-6-new"])
ck("DISCOVER ignores providers we don't use (cohere absent)", "cohere" not in rep["discover"])

print(("[OK]" if not fails else "[FAIL]") + " sync capabilities: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
