"""Live model-catalog validation before dispatch — a stale/rotated model id is caught AT the call site with the
nearest live ids, instead of surfacing as a mystery provider 404.

Grounds spendguard's core discipline (don't guess an identifier — validate against the live source of truth) at
the exact moment it matters: dispatch. Verified against gemini per the incident that motivated it (a dead
gemini-2.5-flash remembered from a past session). Pins:

  (a) pull_live_catalog caches the keyed providers' live id lists (reusing vendor_call.list_models — no new fetch);
  (b) check_model's four outcomes: ok / stale(+nearest ids) / unknown / dormant, with the DECISION being exact
      membership (never a threshold), and can't-verify NEVER a refusal;
  (c) at dispatch (_call_once, metered path) a CONFIRMED-stale id is REFUSED before the SDK is touched, a live id
      PROCEEDS, and an uncovered provider PROCEEDS (warn+proceed) — a transient catalog gap can't block a call;
  (d) the TTL refresh (refresh_catalog_if_stale) skips when fresh and refetches when stale, fail-open.

Offline: vendor_call.list_models and the OpenAI SDK are stubbed; no network, no real key, no spend.
"""
import os
import sys
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["GEMINI_API_KEY"] = "test-key-not-real"        # only gemini is keyed → only gemini gets cataloged
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import catalog, adapters, vendor_call                                 # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# The live gemini ids the provider "serves right now" — deliberately EXCLUDES the dead gemini-2.0-flash.
LIVE_GEMINI = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-flash-latest", "gemini-pro-latest"]
# The REAL Gemini /models endpoint returns ids PREFIXED with `models/`; a request uses the BARE id. The stub
# mirrors that so the test exercises the prefix-normalisation (without it, every live gemini id reads as stale).
vendor_call.list_models = lambda vendor, timeout_s=20: (
    {"vendor": vendor, "models": [{"id": "models/" + i} for i in LIVE_GEMINI], "error": None}
    if vendor == "gemini" else {"vendor": vendor, "models": [], "error": "no key"})

print("-- prefix normalisation: the /models listing form -> the dispatch form --")
ck("`models/gemini-3.7-flash` (listing form) normalises to the bare dispatch id",
   catalog._dispatch_id("models/gemini-3.7-flash") == "gemini-3.7-flash")
ck("a bare id is left unchanged", catalog._dispatch_id("gemini-3.7-flash") == "gemini-3.7-flash")


print("-- (b) DORMANT before any sync: never synced → proceed silently, never a refusal --")
_ok, _s, _st = catalog.check_model("gemini-2.5-flash", "gemini")
ck("no cache → status 'dormant', ok=True (feature dormant until first sync)", _ok is True and _st == "dormant")

print("\n-- (a) pull_live_catalog caches the keyed provider's live ids --")
n_prov, n_ids, errors = catalog.pull_live_catalog()
ck("gemini cataloged with its live ids", n_prov == 1 and n_ids == len(LIVE_GEMINI))
ck("live_model_ids returns the fresh list", set(catalog.live_model_ids("gemini") or []) == set(LIVE_GEMINI))

print("\n-- (b) check_model: ok / stale(+nearest) / unknown --")
_ok, _s, _st = catalog.check_model("gemini-3.7-flash", "gemini")
ck("a live id → 'ok'", _ok is True and _st == "ok")
_ok, _s, _st = catalog.check_model("gemini-2.5-flash", "gemini")
ck("a dead id on a FRESH list → 'stale' (a refusal)", _ok is False and _st == "stale")
ck("...with the nearest LIVE ids as advisory suggestions (all real live ids)", _s and all(x in LIVE_GEMINI for x in _s))
_ok, _s, _st = catalog.check_model("gpt-5.5", "openai")
ck("a provider NOT in the cache → 'unknown', ok=True (can't-verify never refuses)", _ok is True and _st == "unknown")

print("\n-- (c) at DISPATCH: stale REFUSED before the SDK; live PROCEEDS; uncovered PROCEEDS --")
_created = {"calls": 0}


class _Resp:
    choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"), finish_reason="stop")]
    usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=1)


class _OpenAI:
    def __init__(self, *a, **k):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **kw: (_created.__setitem__("calls", _created["calls"] + 1), _created.__setitem__("model", kw.get("model")), _Resp())[-1]))


sys.modules["openai"] = types.SimpleNamespace(OpenAI=_OpenAI)

r_stale = adapters._call_once("gemini-2.5-flash", "hi", max_tokens=16, _skip_lane=True)
ck("a dead gemini id is REFUSED at dispatch (StaleModelId), never sent", r_stale.get("error_type") == "StaleModelId" and _created["calls"] == 0)
ck("...and the refusal names live alternatives + the fix", "sync-catalog" in (r_stale.get("error") or "") and "gemini-" in (r_stale.get("error") or ""))

r_live = adapters._call_once("gemini-3.7-flash", "hi", max_tokens=16, _skip_lane=True)
ck("a LIVE gemini id proceeds to the SDK (not refused)", not r_live.get("error") and _created["calls"] == 1 and _created.get("model") == "gemini-3.7-flash")

# an agy-suffixed id decomposes to a bare id that IS live → also proceeds (validation sees the respelled id)
r_agy = adapters._call_once("gemini-3.7-flash-medium", "hi", max_tokens=16, _skip_lane=True)
ck("an agy-suffixed id → bare live id → proceeds (respell happens before validation)",
   not r_agy.get("error") and _created.get("model") == "gemini-3.7-flash")

# a provider with no catalog coverage must NOT be blocked (warn+proceed). openai isn't cataloged here.
os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
r_unknown = adapters._call_once("gpt-5.5", "hi", max_tokens=16, _skip_lane=True)
ck("an uncovered provider is NOT refused by the catalog (can't-verify → proceed)", r_unknown.get("error_type") != "StaleModelId")
del os.environ["OPENAI_API_KEY"]

print("\n-- (d) TTL refresh: fresh skips, stale refetches, fail-open --")
_pulls = {"n": 0}
_orig_pull = catalog.pull_live_catalog
catalog.pull_live_catalog = lambda *a, **k: (_pulls.__setitem__("n", _pulls["n"] + 1), (1, 4, {}))[-1]
try:
    os.environ["SPENDGUARD_CATALOG_REFRESH_HOURS"] = "12"
    res_fresh = catalog.refresh_catalog_if_stale()     # cache just written above → fresh
    ck("a fresh cache is not refetched", res_fresh.get("fresh") is True and _pulls["n"] == 0)
    os.environ["SPENDGUARD_CATALOG_REFRESH_HOURS"] = "0"
    ck("refresh_hours=0 disables the auto-refresh", catalog.refresh_catalog_if_stale().get("skipped") and _pulls["n"] == 0)
finally:
    catalog.pull_live_catalog = _orig_pull
    os.environ.pop("SPENDGUARD_CATALOG_REFRESH_HOURS", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_validation: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
