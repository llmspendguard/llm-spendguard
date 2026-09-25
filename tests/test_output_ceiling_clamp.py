"""The output budget SENT is the model's CEILING (adapters.output_budget → pricing.output_ceiling; docs/CANONICAL_
CONCERNS.json) — the caller's provided max_tokens AND the per-class `recommend` are BOTH IGNORED. max_output is billed on
ACTUAL tokens, so the budget is the model's real maximum (or the 32K floor when unknown), and nothing a caller or a
predictor supplies can lower it or inflate it past the endpoint.

The incident: a bulkgate `recommend` of 146,576 (above the model's real 128,000 ceiling) was applied as
max(caller, recommend) and sent, 400ing the call; and a poisoned learned fact of 2000 under-truncated every answer.
The doctrine removed BOTH inputs from the budget: the caller's number and the recommend are not consulted; only the
authoritative ceiling (published limits → live /models catalog → learned fact FLOORED to 32K → the 32K floor backstop).

Pins that resolution + that the caller/recommend are ignored:
  (a) an over-ceiling recommend (146,576) never reaches the wire — the PUBLISHED ceiling (128,000) is sent;
  (b) a poisoned LOW fact is IGNORED when the catalog knows the ceiling (the 2000 under-truncation case);
  (c) the live-/models ceiling is used when the synced cache lacks the model;
  (d) a truly-unknown ceiling → the 32K FLOOR ("if it is not published, the floor is 32000"), never 128K and never poison.

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

print("\n-- (d) a truly-unknown ceiling → the 32K FLOOR ('if it is not published, the floor is 32000') --")
# Not the 128K backstop and not the poisoned 146576 recommend (which is never consulted): an unknown model gets the
# conservative floor — no truncation below it, and safe for a no-heal lane that can't recover a 400 on an over-large
# budget. Publishing the real max (the price data) is how an unknown model earns a higher ceiling.
ck("no published/catalog/fact → the 32K floor, never 128K and never the poisoned 146576",
   _budget("gpt-6-unreleased", 7000, None, None, None) == adapters.TOKEN_FLOOR)

print("\n-- the fact is only a LAST resort (no catalog ceiling at all) --")
ck("published None + catalog None → the fact is used", _budget("gpt-4-legacy", 7000, None, None, 32000) == 32000)

print(f"\n{'[FAIL]' if fails else 'OK'} test_output_ceiling_clamp: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
