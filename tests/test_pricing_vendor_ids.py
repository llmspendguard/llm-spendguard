"""Vendor-qualified price resolution — the bug that made the 2,400-model breadth layer unreachable.

The synced LiteLLM table keys most non-first-party models as `vendor/model` (`moonshot/kimi-k2.5`,
`zai/glm-4.6`) while callers pass the bare id their SDK takes. Before this fix EVERY GLM/Kimi call raised
"no canonical price" with a real published rate sitting in the cache — and the only GLM that priced did so
from a hand-typed STUB that under-priced the real z.ai 5-series by ~40%. Rules under test:
  • bare id resolves against `vendor/model` keys (raw first — `kimi-latest` is a real id, not an alias);
  • `provider:model` and provider= pin the vendor exactly;
  • DEEP reseller paths (bedrock/<region>/…, cloudflare/@cf/…) are a different vendor's resale rate → never
    used for a bare id;
  • vendors that disagree on $ raise (ambiguous) instead of picking — spendguard never guesses money;
  • an unpriced id still fails LOUD.
Offline: PRICING is stubbed, no network, no cache dependency.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-vendorprice-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import pricing, adapters

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def rates(i, o):
    return {"in_": i, "out": o, "cached_in": i / 10, "batch_in": i / 2, "batch_out": o / 2}


def raises(fn):
    try:
        fn()
        return False
    except KeyError:
        return True


_SAVED = dict(pricing.PRICING)
pricing.PRICING.clear()
pricing.PRICING.update({
    "gpt-5.5": rates(5.0, 30.0),                      # first-party, bare key (unchanged behavior)
    "moonshot/kimi-k2.5": rates(0.6, 3.0),
    "moonshot/kimi-latest": rates(2.0, 5.0),          # a REAL id ending in -latest (normalize would eat it)
    "azure_ai/kimi-k2.5": rates(0.6, 3.0),            # same $ → unambiguous
    "bedrock/ap-south-1/moonshotai.kimi-k2.5": rates(0.72, 3.6),   # deep reseller path — must be ignored
    "zai/glm-4.6": rates(0.6, 2.2),
    "zai/glm-5": rates(1.0, 3.2),
    "openrouter/split-model": rates(1.0, 2.0),        # two vendors, DIFFERENT $ → ambiguous
    "together/split-model": rates(4.0, 8.0),
    "vendor/dated-model-2026-01-15": rates(3.0, 9.0),
})

print("-- bare vendor-hosted ids now price (each of these raised KeyError before) --")
check("kimi-k2.5 → $0.6/$3.0", pricing.price("kimi-k2.5")["in_"] == 0.6)
check("glm-4.6 → $0.6/$2.2", pricing.price("glm-4.6")["out"] == 2.2)
check("glm-5 → the REAL z.ai rate $1.0/$3.2 (the stub said 0.6/2.2)", pricing.price("glm-5")["in_"] == 1.0)
check("kimi-latest resolves RAW, not via the -latest strip", pricing.price("kimi-latest")["in_"] == 2.0)
check("first-party bare keys unaffected", pricing.price("gpt-5.5")["in_"] == 5.0)
check("dated snapshot still normalizes into a vendor key",
      pricing.price("dated-model-2026-01-15")["in_"] == 3.0 or pricing.price("vendor/dated-model-2026-01-15")["in_"] == 3.0)

print("-- the vendor can be pinned explicitly (no inference at all) --")
check("provider= pins", pricing.price("kimi-k2.5", provider="moonshot")["in_"] == 0.6)
check("'provider:model' form pins", pricing.price("moonshot:kimi-k2.5")["in_"] == 0.6)
check("a pinned vendor that doesn't publish the model still resolves the unambiguous vendor entry",
      pricing.price("kimi-k2.5", provider="zai")["in_"] == 0.6)

print("-- resale paths never price a bare id --")
check("deep reseller key excluded (0.72 never returned)", pricing.price("kimi-k2.5")["in_"] != 0.72)

print("-- disagreeing vendors RAISE rather than pick a number --")
try:
    pricing.price("split-model")
    check("ambiguous vendors raise", False)
except KeyError as e:
    check("ambiguous vendors raise with both named + how to fix",
          "Ambiguous" in str(e) and "openrouter" in str(e) and "together" in str(e))
check("pinning resolves the ambiguity", pricing.price("split-model", provider="together")["in_"] == 4.0)

print("-- unpriced is still LOUD (never a guessed or $0 number) --")
try:
    pricing.price("no-such-model-anywhere")
    check("unknown model raises", False)
except KeyError as e:
    check("unknown model raises pointing at sync-prices + source rule",
          "sync-prices" in str(e) and "DO NOT guess" in str(e))

print("-- cost helpers thread the vendor through --")
check("realtime_cost prices a bare vendor id",
      abs(pricing.realtime_cost("kimi-k2.5", 1_000_000, 100_000) - (0.6 + 0.3)) < 1e-9)
check("batch_cost = 50% of realtime rates",
      abs(pricing.batch_cost("kimi-k2.5", 1_000_000, 0) - 0.3) < 1e-9)

pricing.PRICING.clear(); pricing.PRICING.update(_SAVED)

print("-- Moonshot/Kimi is a registered provider, family-wide --")
p = adapters.PROVIDERS.get("moonshot")
check("registered with its own key env", p and p["key_env"] == "MOONSHOT_API_KEY")
check("OpenAI-compatible base_url", p and p["kind"] == "openai" and "moonshot" in (p["base_url"] or ""))
for m in ("kimi-k2.5", "kimi-k2.6", "kimi-latest", "kimi-k3-future-id", "moonshot-v1-32k"):
    check(f"{m} routes to moonshot", adapters.provider_for(m) == "moonshot")
check("GLM still routes to zai (unchanged)", adapters.provider_for("glm-5.2") == "zai")
check("claude/gpt routing untouched",
      adapters.provider_for("claude-opus-4-8") == "anthropic" and adapters.provider_for("gpt-5.5") == "openai")

print("-- no fabricated rates ship in prices.json (the stub that under-priced GLM by 40%) --")
import json
pj = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "src", "spendguard", "prices.json")))
# The rule this guard exists for is "never ship an INVENTED rate" — it was written as "zai must be empty",
# which cannot tell an invented number from a cited one. Now that two models are genuinely absent from the
# synced table (glm-5.2, kimi-k3) and carry the vendor's own pricing URL, the guard tests the INTENT instead:
# every curated entry must cite a source. That is STRICTER than emptiness — an uncited entry now fails
# wherever it appears, not just under zai.
# Provenance lives at ONE of two levels, and the guard must know which: the legacy openai/anthropic tables
# cite a verified URL + date in _meta ("<prov>_source"), while a provider with no file-level source must cite
# per entry. Requiring per-entry everywhere false-flagged 11 Anthropic rows that ARE documented; requiring it
# nowhere is how a fabricated stub shipped. So: an entry is traceable if IT cites a source, or its provider does.
_meta = pj.get("_meta") or {}
uncited = []
for _prov, _blk in (pj.get("providers") or {}).items():
    _prov_cited = bool(str(_meta.get(f"{_prov}_source") or "").strip())
    for _m, _r in (_blk.get("models") or {}).items():
        if isinstance(_r, dict) and not _prov_cited and not str(_r.get("_source") or "").strip():
            uncited.append(f"{_prov}/{_m}")
if uncited:
    print(f"      uncited entries: {uncited[:6]}")
check("every shipped curated rate cites its source (no fabricated stubs, anywhere)", not uncited)
_zai = ((pj.get("providers") or {}).get("zai") or {}).get("models") or {}
check("the zai entries that DO ship are the sync-absent ones, each with a URL",
      all("http" in (r.get("_source") or "") for r in _zai.values()))
blob = json.dumps(pj).lower()
check("no 'unverified stub' anywhere in the shipped price table", "unverified stub" not in blob)
check("an unknown-vendor id is loud, not guessed", raises(lambda: pricing.price("totally-unknown-xyz")))

print(f"\n{'[FAIL]' if failures else 'OK'} test_pricing_vendor_ids: {failures} failure(s)")
sys.exit(1 if failures else 0)
