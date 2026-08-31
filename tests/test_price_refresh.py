"""Offline tests for the DAILY PRICE AUTO-REFRESH (sync.refresh_if_stale) — the LiteLLM breadth layer keeps
itself current by riding `saas sync` (which the installed `spendguard schedule` agent runs on a cadence):
re-fetch ONLY when the cache is older than pricing.refresh_days (so an hourly agent still refreshes at most
once a day), strictly fail-open (a failed fetch keeps the existing cache + curated prices), 0 disables.
NO network: sync.sync is monkeypatched; the cache file is seeded with controlled _fetched timestamps.
"""
import os, sys, json, tempfile, datetime
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import sync as price_sync

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


CALLS = {"n": 0}
def fake_sync():
    CALLS["n"] += 1
    return 2700, ["note: fixture"]
price_sync.sync = fake_sync


def seed_cache(age_hours):
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)
    os.makedirs(os.path.dirname(price_sync.CACHE), exist_ok=True)
    json.dump({"_fetched": ts.isoformat(timespec="seconds"), "models": {}}, open(price_sync.CACHE, "w"))


print("-- no cache at all → fetches --")
if os.path.exists(price_sync.CACHE):
    os.unlink(price_sync.CACHE)
check("cache_age_days None when absent", price_sync.cache_age_days() is None)
r = price_sync.refresh_if_stale()
check("missing cache triggers a fetch", r.get("refreshed") and CALLS["n"] == 1)
check("models count surfaced", r.get("models") == 2700)

print("-- fresh cache → NO fetch (an hourly scheduler must not refetch hourly) --")
seed_cache(age_hours=2)
r = price_sync.refresh_if_stale()
check("fresh (2h < 1d) skips the fetch", r.get("fresh") and CALLS["n"] == 1)
check("age reported", 0 < r.get("age_days", -1) < 0.2)

print("-- stale cache → fetches once --")
seed_cache(age_hours=30)
r = price_sync.refresh_if_stale()
check("stale (30h > 1d) refetches", r.get("refreshed") and CALLS["n"] == 2)

print("-- knob: pricing.refresh_days honors env override; 0 disables entirely --")
os.environ["SPENDGUARD_PRICES_REFRESH_DAYS"] = "2"
seed_cache(age_hours=30)
r = price_sync.refresh_if_stale()
check("30h < 2d window → fresh, no fetch", r.get("fresh") and CALLS["n"] == 2)
os.environ["SPENDGUARD_PRICES_REFRESH_DAYS"] = "0"
seed_cache(age_hours=24 * 400)
r = price_sync.refresh_if_stale()
check("0 = never auto-refresh, even a year stale", r.get("skipped") and CALLS["n"] == 2)
del os.environ["SPENDGUARD_PRICES_REFRESH_DAYS"]

print("-- fail-open: a failed fetch reports the error and leaves the cache untouched --")
def broken_sync():
    CALLS["n"] += 1
    raise RuntimeError("network down")
price_sync.sync = broken_sync
seed_cache(age_hours=30)
before = open(price_sync.CACHE).read()
r = price_sync.refresh_if_stale()
check("error surfaced, not raised", "network down" in (r.get("error") or ""))
check("existing cache untouched on failure", open(price_sync.CACHE).read() == before)

print("-- per-UNIT rates flow LiteLLM → cache `unit_models` → pricing._load_units (the missing pipe) --")
RAW = {
    "whisper-x": {"input_cost_per_second": 0.0001, "litellm_provider": "openai"},
    "tts-x": {"input_cost_per_character": 1.5e-05, "litellm_provider": "openai"},
    "dalle-x": {"input_cost_per_image": 0.04, "litellm_provider": "openai"},
    "gpt-x": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "litellm_provider": "openai"},
    "ctx-x": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "max_input_tokens": 400000,
              "litellm_provider": "openai"},
    "free-x": {"input_cost_per_token": 0, "output_cost_per_token": 0, "litellm_provider": "openai"},
    "moon-x": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "litellm_provider": "moonshot"},
    "pub-x": {"input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06, "litellm_provider": "moonshot",
              "input_cost_per_token_batches": 1e-07, "output_cost_per_token_batches": 2e-07},
    "sample_spec": {"input_cost_per_token": 1},
}
models, provs, unit_models, context, zero_rate = price_sync._convert(RAW)
check("unit-billed entries captured even with NO token rate",
      set(unit_models) == {"whisper-x", "tts-x", "dalle-x"})
check("token model still converts (and is not a unit model)", "gpt-x" in models and "gpt-x" not in unit_models)
# Context LIMITS ride along with the prices (the upstream file carries both). They are what makes the
# impossible-estimate rail possible — see tests/test_estimate_plausibility.py.
check("context limits are passed through when present", context.get("ctx-x", {}).get("max_input_tokens") == 400000)
check("a model with no limit published gets NO invented one", "gpt-x" not in context)
# A ZERO rate is not a price, it is a MISSING one — and caching it is worse than caching nothing, because
# price() then SUCCEEDS and real spend records at $0.00 with no warning at all. 122 upstream entries carry
# zero rates; any of them would have silently swallowed spend.
check("a zero-rate entry is NOT cached as a price", "free-x" not in models)
check("…and is reported, not dropped in silence", "free-x" in zero_rate)
# Moonshot bills batch at 60% of standard, not the 50% every other provider uses. The generic fallback
# UNDER-priced every Moonshot batch by 17%, and under-pricing is the dangerous direction for a spend gate.
check("a provider with its own published batch fraction uses it, not the generic 50%",
      abs(models["moon-x"]["batch_in"] - 0.6) < 1e-9)
check("everyone else still gets the 50% convention", abs(models["gpt-x"]["batch_in"] - 0.5) < 1e-9)
check("an explicitly PUBLISHED batch rate still wins over any fraction",
      abs(models["pub-x"]["batch_in"] - 0.1) < 1e-9)
os.makedirs(os.path.dirname(price_sync.CACHE), exist_ok=True)
json.dump({"_fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "models": models, "providers": provs, "unit_models": unit_models,
           "context": context}, open(price_sync.CACHE, "w"))
from spendguard import pricing
u = pricing._load_units()
check("audio_second: whisper $0.0001/s from the cache", abs(u["audio_second"].get("whisper-x", 0) - 0.0001) < 1e-9)
check("tts_char: $0.000015/char from the cache", abs(u["tts_char"].get("tts-x", 0) - 1.5e-05) < 1e-12)
check("image: dall-e $0.04/image from the cache", abs(u["image"].get("dalle-x", 0) - 0.04) < 1e-9)

print("-- schema: pricing.refresh_days is a documented knob --")
from spendguard import config_schema
k = [o for o in config_schema.SETTINGS if o.get("section") == "pricing" and o.get("key") == "refresh_days"]
check("knob present with env + default 1",
      k and k[0].get("env") == "SPENDGUARD_PRICES_REFRESH_DAYS" and k[0].get("default") == 1)

print("-- _validate cross-checks the FULL curated table vs LiteLLM (not 3 hardcoded anchors) --")
# Build a LiteLLM fetch that matches EVERY curated price except one tampered model, keyed bare as LiteLLM often
# does. Derived from pricing._FALLBACK so this test can never drift from the curated table it guards.
fake = {}
for _m, _r in pricing._FALLBACK.items():
    if _r.get("in_") is None:
        continue
    fake[_m] = {"in_": _r["in_"], "out": _r["out"], "cached_in": 0, "batch_in": 0, "batch_out": 0}
for _i in range(1000):                                   # pad past the >=1000 structural sanity gate
    fake[f"pad-{_i}"] = {"in_": 1.0, "out": 2.0}
TAMPER = "claude-opus-4-8"
fake[TAMPER] = {"in_": 999.0, "out": 999.0}              # LiteLLM "disagrees" with curated 5/25 on this one
ok, msgs = price_sync._validate(fake)
check("_validate does not abort a healthy fetch", ok is True)
_checked = int(msgs[0].split("/")[1].split()[0])
check("cross-check covers the FULL curated table (>3 models, not the old 3 anchors)", _checked > 3)
check("a real per-token disagreement is surfaced as a DIFF (curated wins, re-verify)",
      any(TAMPER in d and d.startswith("DIFF") and "re-verify" in d for d in msgs[1:]))
check("only the tampered model is flagged — the agreeing ones are not false-positived",
      sum(1 for d in msgs[1:] if d.startswith("DIFF")) == 1)
check("a structurally tiny fetch is still REFUSED (sanity gate holds, never caches junk)",
      price_sync._validate({"only": {"in_": 1, "out": 2}})[0] is False)
check("no inline hardcoded price anchor remains in _validate (source-of-record is pricing._FALLBACK)",
      "5.0, 30.0" not in open(price_sync.__file__).read() and "0.15, 0.60" not in open(price_sync.__file__).read())

print(f"\n{'[FAIL]' if failures else 'OK'} test_price_refresh: {failures} failure(s)")
sys.exit(1 if failures else 0)
