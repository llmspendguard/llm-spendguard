"""Pre-dispatch served-list validation — a stale/rotated model id is caught at the call site with the SAME-model
live alternative, instead of surfacing as a mystery provider 404.

Wires the ALREADY-EXISTING but caller-less primitive vendor_call.serves() into a dispatch pre-flight at the one
choke point every vendor/lane path inherits (adapters._call_once — vendor_call.call routes through it too),
mirroring the input-window pre-flight already in vendor_call.call. Pins:

  (a) list_models returns the DISPATCH form — _dispatch_form strips Gemini's `models/` listing prefix, the bug
      that made serves() report every served Gemini model ABSENT (it had zero callers, so it never surfaced);
  (b) served_check is CACHE-FIRST ($0, no per-call latency), but a cache MISS is CONFIRMED LIVE before it is ever
      called 'stale' — a stale cache can never cause a false rejection;
  (c) 'unchecked' (no cached list, or live discovery unavailable) PROCEEDS — a can't-check is never a 'no';
  (d) the closest-alternative is AGENTIC (pricing._same_model_as_ours, fact_key='served_id'), never string-distance;
  (e) at dispatch: a confirmed-stale id is REFUSED (StaleModelId) before the SDK, a served id proceeds, an
      unlistable vendor proceeds.

Offline: vendor_call.list_models and the agentic resolver are stubbed; no network, no key, no spend.
"""
import os
import sys
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-served-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["GEMINI_API_KEY"] = "test-key-not-real"        # only gemini is keyed → only gemini gets cataloged
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import vendor_call, catalog, adapters, pricing                         # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- (a) list_models returns the DISPATCH form (Gemini `models/` prefix stripped at the source) --")
ck("_dispatch_form strips the listing prefix", vendor_call._dispatch_form("models/gemini-3.7-flash") == "gemini-3.7-flash")
ck("_dispatch_form leaves a bare id unchanged", vendor_call._dispatch_form("gemini-3.7-flash") == "gemini-3.7-flash")

# The live served list the stub returns (dispatch form already, as list_models would after normalisation).
LIVE = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-flash-latest"]
_served = {"ids": list(LIVE)}     # mutable so a later test can diverge the LIVE list from the seeded cache


def _stub_list_models(vendor, timeout_s=20):
    if vendor == "gemini":
        return {"vendor": vendor, "models": [{"id": i} for i in _served["ids"]], "error": None}
    return {"vendor": vendor, "models": [], "error": "no key"}


vendor_call.list_models = _stub_list_models
catalog.pull_live_catalog()                               # seed the cache from the (stubbed) live list

print("\n-- (b) served_check is cache-first, and a cache MISS is confirmed live before 'stale' --")
ck("a served id resolves from cache ($0 fast path) → 'served'", vendor_call.served_check("gemini", "gemini-3.7-flash") == "served")
ck("a dead id (cache miss, live-confirmed absent) → 'stale'", vendor_call.served_check("gemini", "gemini-2.0-flash") == "stale")
# STALE-CACHE SAFETY: the provider just added a model the cache doesn't have. A cache miss must be confirmed LIVE,
# so the newly-served id is NOT falsely rejected.
_served["ids"] = LIVE + ["gemini-4-flash"]               # live now serves it; the seeded cache does NOT
ck("a served-but-not-yet-cached id is confirmed live, never false-rejected", vendor_call.served_check("gemini", "gemini-4-flash") == "served")
_served["ids"] = list(LIVE)

print("\n-- (c) can't-check is never a rejection --")
ck("an uncached vendor → 'unchecked' (dormant, no per-call live fetch)", vendor_call.served_check("openai", "gpt-5.5") == "unchecked")
# cache HAS gemini but not this id, AND live discovery now fails → 'unchecked', never 'stale'
vendor_call.list_models = lambda vendor, timeout_s=20: {"vendor": vendor, "models": [], "error": "network down"}
ck("a cache miss whose live confirm FAILS → 'unchecked', not 'stale'", vendor_call.served_check("gemini", "gemini-9-flash") == "unchecked")
vendor_call.list_models = _stub_list_models

print("\n-- (d) the closest-alternative is AGENTIC (_same_model_as_ours), never string-distance --")
class _SameModelStub:
    """Stands in for the agentic resolver; records the call so the test can assert it was asked the right way, and
    returns a {our_id: their_id} map judging each stale id's same-model to be the served gemini-3.7-flash. Capture
    lives in INSTANCE state (not a shared module container)."""
    def __init__(self):
        self.fact_key = self.run = None

    def __call__(self, ours, their_ids, run=False, advisor=None, fact_key="openrouter_id"):
        self.fact_key, self.run = fact_key, run
        return {o: "gemini-3.7-flash" for o in ours}, {"agentic": len(ours)}


_same_stub = _SameModelStub()
pricing._same_model_as_ours = _same_stub
same, live_ids = vendor_call.closest_served("gemini", "gemini-3.6-flash")
ck("closest_served returns the agentic SAME-model id", same == "gemini-3.7-flash")
ck("...decided via _same_model_as_ours with its own fact_key ('served_id'), run=True",
   _same_stub.fact_key == "served_id" and _same_stub.run is True)
ck("...over the live served ids (not a hardcoded list)", set(live_ids) == set(LIVE))

print("\n-- (e) at DISPATCH: stale REFUSED before the SDK; served + unlistable PROCEED --")
_created = {"n": 0}
_resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")],
                              usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=1))
sys.modules["openai"] = types.SimpleNamespace(OpenAI=lambda *a, **k: types.SimpleNamespace(
    chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: (_created.__setitem__("n", _created["n"] + 1), _resp)[-1]))))

r_stale = adapters._call_once("gemini-2.0-flash", "hi", max_tokens=16, _skip_lane=True)
ck("a dead gemini id is REFUSED at dispatch (StaleModelId), never sent", r_stale.get("error_type") == "StaleModelId" and _created["n"] == 0)
ck("...the refusal names the agentic same-model alternative", "gemini-3.7-flash" in (r_stale.get("error") or ""))

r_live = adapters._call_once("gemini-3.7-flash", "hi", max_tokens=16, _skip_lane=True)
ck("a served gemini id proceeds to the SDK (not refused)", not r_live.get("error") and _created["n"] == 1)

os.environ["OPENAI_API_KEY"] = "sk-test-not-real"        # openai is uncached here → 'unchecked' → must proceed
r_unlistable = adapters._call_once("gpt-5.5", "hi", max_tokens=16, _skip_lane=True)
ck("an uncached/unlistable vendor is NOT refused by the pre-flight", r_unlistable.get("error_type") != "StaleModelId")
del os.environ["OPENAI_API_KEY"]

print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_validation: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
