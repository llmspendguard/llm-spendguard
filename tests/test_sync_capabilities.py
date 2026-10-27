"""LiteLLM-sourced capability sync — the GROUND-TRUTH replacement for guessing vision capability (an LLM judge
can't flag models past its cutoff; a hand-list drifts). Covers: (A) _litellm_record prefers the daily-refreshed
CACHE (capabilities + context merged) over the installed package; (B) _updates_for maps a record → vision +
capabilities + fills only MISSING limits (never overwrites curated); (C) audit_catalog_completeness reports coverage
+ uncovered + discovers provider models not in the catalog. Offline (cache + litellm lookup controlled), zero spend."""
import json
import os
import sys
from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix="spendguard-synccaps-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import sync_capabilities as sc, sync
from spendguard import model_catalog as catalog_contract

_REAL_LR = sc._litellm_record          # captured before section B monkeypatches it — section E needs the real one

fails = []



# ── A. _litellm_record prefers the daily-github CACHE, merging its capabilities + context (limits) sections ──
_cache = os.path.join(os.environ["SPENDGUARD_HOME"], "litellm_prices.json")
with open(_cache, "w") as fh:
    json.dump({"capabilities": {"cachehit": {"supports_vision": True, "mode": "chat"}},
               "context": {"cachehit": {"max_input_tokens": 200000, "max_output_tokens": 64000}}}, fh)
sync.CACHE = _cache                                        # point the reader at our controlled cache
rec = sc._litellm_record("cachehit", None)
record_script_check(fails, "cache-read MERGES capabilities + context (limits) into one record",
   rec.get("supports_vision") is True and rec.get("max_input_tokens") == 200000 and rec.get("max_output_tokens") == 64000)
record_script_check(fails, "a model absent from the cache → None from the cache path (before any package fallback)",
   sc._litellm_record("not-in-cache-xyz", None) is None)

# ── B. _updates_for maps a record → vision + capabilities, fills only MISSING limits ──
sc._fetch_server_catalog = lambda: {}                     # OFFLINE: no server this run (server-first tier tested in E)
_LL = {
    "vmodel": {"supports_vision": True, "supports_response_schema": True, "supports_function_calling": True,
               "mode": "chat", "max_input_tokens": 200000, "max_output_tokens": 64000},
    "tmodel": {"supports_vision": False, "mode": "chat", "max_input_tokens": 32000, "max_output_tokens": 8000},
}
sc._litellm_record = lambda mid, metered, server_cat=None: _LL.get(mid) or _LL.get(metered)   # pin (3-arg: server_cat)
models = {
    "vmodel": {"metered_id": "vmodel"},
    "tmodel": {"metered_id": "tmodel", "output_ceiling": {"value": 4096, "source": "curated-verified"}},
    "umodel": {"metered_id": "umodel"},                    # not in _LL → uncovered
}
updates, summary = sc._updates_for(models)
record_script_check(fails, "vision True + capabilities block recorded",
   updates.get("vmodel", {}).get("vision") is True
   and updates["vmodel"].get("capabilities", {}).get("response_schema") is True)
record_script_check(fails, "MISSING limits filled from LiteLLM (source=litellm)",
   updates["vmodel"].get("output_ceiling") == {"value": 64000, "source": "litellm"})
record_script_check(fails, "a CURATED limit is NEVER overwritten", "output_ceiling" not in updates.get("tmodel", {}))
record_script_check(fails, "uncovered model → no update (never a guessed flag)", "umodel" not in updates)
record_script_check(fails, "summary counts", summary["covered"] == 2 and summary["uncovered"] == 1
   and summary["vision_true"] == 1 and summary["vision_false"] == 1)

# The shared writer owns freshness: all tracked groups advance; no-op, untouched, and editorial-only changes do not.
for field, value in [("price", {"in_": 2}), ("vision", True), ("capabilities", {"mode": "chat"}),
                     ("context_window", {"value": 99}), ("output_ceiling", {"value": 9})]:
    row = {"id": "x", "provider": "p", "as_of": "2025-01-01"}
    catalog_contract.merge_row_updates(row, {field: value}, today="2026-10-12")
    record_script_check(fails, f"tracked {field} change advances as_of", row["as_of"] == "2026-10-12")
row = {"id": "x", "provider": "p", "vision": True, "as_of": "2025-01-01"}
catalog_contract.merge_row_updates(row, {"vision": True}, today="2026-10-12")
record_script_check(fails, "no-op tracked observation retains as_of", row["as_of"] == "2025-01-01")
catalog_contract.merge_row_updates(row, {"aliases": ["editorial"]}, today="2026-10-12")
record_script_check(fails, "routing/editorial-only update retains as_of", row["as_of"] == "2025-01-01")
untouched = {"id": "y", "provider": "p", "as_of": "2024-02-03"}
record_script_check(fails, "untouched row retains as_of", untouched["as_of"] == "2024-02-03")

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
record_script_check(fails, "coverage counts covered vs uncovered", rep["catalog_n"] == 2 and rep["covered"] == 1 and rep["uncovered"] == ["custommodel"])
record_script_check(fails, "no_vision lists models with no vision verdict", "custommodel" in rep["no_vision"])
record_script_check(fails, "DISCOVER surfaces a provider model not in the catalog (gpt-6-new)", rep["discover"].get("openai") == ["gpt-6-new"])
record_script_check(fails, "DISCOVER ignores providers we don't use (cohere absent)", "cohere" not in rep["discover"])

# ── D. model_catalog breadth fallback: a model NOT in the curated 54 still resolves via the LiteLLM cache ──
# (this is what makes "everything we use/might use" covered — the catalog is the curated OVERRIDE, cache is breadth)
with open(_cache, "w") as fh:
    json.dump({"capabilities": {"cache-only-vmodel": {"supports_vision": True},
                                "cache-only-tmodel": {"supports_vision": False}}, "context": {}, "providers": {}}, fh)
record_script_check(fails, "a NON-catalog model resolves vision from the cache breadth (everything we use/might use)",
   mc.vision_capable("cache-only-vmodel") is True)
record_script_check(fails, "the cache breadth also carries a False verdict", mc.vision_capable("cache-only-tmodel") is False)
record_script_check(fails, "a model in NEITHER catalog nor cache → None (unknown, never assumed)", mc.vision_capable("nowhere-model-xyz") is None)
record_script_check(fails, "model_capability returns None for a capability absent from the cache record",
   mc.model_capability("cache-only-vmodel", "response_schema") is None)

# ── E. SERVER-FIRST tier: the org-authoritative /v1/models catalog is checked BEFORE the local cache, mapped to the
#        litellm shape; a server MISS falls through to local; the server fetch is FAIL-OPEN (unreachable → local). ──
record_script_check(fails, "_server_row_to_litellm maps capabilities + mode + upper bounds to the litellm shape",
   sc._server_row_to_litellm({"capabilities": {"supports_vision": True}, "mode": "chat",
                              "max_input_tokens": 128000, "max_output_tokens": 16000})
   == {"supports_vision": True, "mode": "chat", "max_input_tokens": 128000, "max_output_tokens": 16000})
# server_cat carries the model → returned FIRST (its supports_vision), ahead of the local cache written above
_srv = {"srv-model": {"capabilities": {"supports_vision": True}, "mode": "chat", "max_output_tokens": 4242}}
_e = _REAL_LR("srv-model", None, _srv)
record_script_check(fails, "_litellm_record returns the SERVER record first (server-authoritative)",
   _e is not None and _e.get("supports_vision") is True and _e.get("max_output_tokens") == 4242)
record_script_check(fails, "a server MISS falls through (server_cat lacks it → None here, no local/pkg match)",
   _REAL_LR("not-on-server", None, _srv) is None)
# _fetch_server_catalog is FAIL-OPEN: saas NOT configured (ready→False) → {} with no network call
from spendguard import saas as _saas          # _fetch_server_catalog does `from . import saas` → patch the module
_saas.ready = lambda: (False, "unconfigured")
record_script_check(fails, "_fetch_server_catalog → {} when saas is unconfigured (no server to check)", sc._fetch_server_catalog() == {})
# saas CONFIGURED but the fetch fails → {} (loud), never a raise into the caller
_saas.ready = lambda: (True, "ok")
_saas.fetch_models = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("server down"))
record_script_check(fails, "_fetch_server_catalog → {} (fail-open) when a CONFIGURED server is unreachable", sc._fetch_server_catalog() == {})

print(("[OK]" if not fails else "[FAIL]") + " sync capabilities: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
