"""The output budget is ALWAYS clamped to the model's published ceiling — output = min(max(provided|predicted,
floor), model_max) — and model_max comes from the CATALOG, never the poison-prone per-model max_output fact.

The incident: a bulkgate `recommend` of 146,576 (above the max output EVER observed for the class, 58,296, and
above the model's real 128,000 ceiling) was applied as max(caller, recommend) and sent, because the clamp read
pricing.max_output — the auto-heal FACT — which was None for gpt-5.4-nano (no clamp → 400 every call) and a
poisoned 2000 for gpt-5-nano (clamp → under-truncate every answer). The published ceiling was in the catalog the
whole time (pricing.max_output_tokens('gpt-5.4-nano') == 128000); the clamp just wasn't reading it.

Pins the resolution order — pricing.max_output_tokens (synced limits catalog) → catalog.model_ceiling (live
/models) → the learned fact only as a LAST resort — and that:
  (a) an over-ceiling predicted/recommend is clamped DOWN to the published ceiling (the 146,576 → 128,000 case);
  (b) a poisoned LOW fact is IGNORED when the catalog knows the ceiling (the 2000 under-truncation case);
  (c) the live-/models ceiling is used when the synced cache lacks the model;
  (d) a truly-unknown ceiling is NOT clamped (the downward heal handles it) — a can't-know is never a wrong cap.

Offline: the ceiling sources, the predictor, and the raw sender are stubbed; no network, no model call.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-ceil-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, pricing, bulkgate, catalog                            # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# The poisoned predictor: recommend far above any real output AND above the model ceiling.
bulkgate.maxtokens = lambda sig: {"recommend": 146576}

# Capture the budget that actually reaches the raw sender (what would go to the provider).
_captured = {}


def _fake_once(model, prompt, max_tokens=None, **kw):
    _captured["budget"] = max_tokens
    return {"text": "ok", "error": None, "finish_reason": "stop", "out_tok": 3}


adapters._call_once = _fake_once


def _budget(model, provided, published, cat_ceiling, fact):
    pricing.max_output_tokens = lambda m: published
    pricing.max_output = lambda m: fact
    catalog.model_ceiling = lambda v, m: cat_ceiling
    _captured.clear()
    adapters.call(model, "hi", max_tokens=provided, sig="test:ceiling")
    return _captured.get("budget")


print("-- (a) an over-ceiling predicted budget is clamped DOWN to the published ceiling --")
ck("min(max(7000,146576), 128000) == 128000 — no 400", _budget("gpt-5.4-nano", 7000, 128000, None, None) == 128000)

print("\n-- (b) a poisoned LOW fact is IGNORED when the catalog knows the ceiling --")
ck("published 128000 wins over a poisoned fact 2000 (no under-truncation)",
   _budget("gpt-5-nano", 7000, 128000, None, 2000) == 128000)

print("\n-- (c) the live-/models ceiling is used when the synced cache lacks the model --")
# a known-provider id (gpt- → openai) so provider_for resolves and the per-provider catalog ceiling is consulted
ck("published None → catalog.model_ceiling 64000 clamps", _budget("gpt-5-experimental", 7000, None, 64000, None) == 64000)

print("\n-- (d) a truly-unknown ceiling is capped at the absolute MAX_TOKEN_CEILING backstop (never the poison) --")
# The backstop gives the Anthropic path (no downward heal) the same protection as OpenAI-compat: a poisoned
# recommend can never send an absurd budget on ANY provider; the OpenAI heal still recovers a genuinely-lower one.
ck("no published/catalog/fact → capped at MAX_TOKEN_CEILING, not the poisoned 146576",
   _budget("gpt-6-unreleased", 7000, None, None, None) == adapters.MAX_TOKEN_CEILING)

print("\n-- the fact is only a LAST resort (no catalog ceiling at all) --")
ck("published None + catalog None → the fact is used", _budget("gpt-4-legacy", 7000, None, None, 32000) == 32000)

print(f"\n{'[FAIL]' if fails else 'OK'} test_output_ceiling_clamp: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
