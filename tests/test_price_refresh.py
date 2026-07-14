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
    "sample_spec": {"input_cost_per_token": 1},
}
models, provs, unit_models = price_sync._convert(RAW)
check("unit-billed entries captured even with NO token rate",
      set(unit_models) == {"whisper-x", "tts-x", "dalle-x"})
check("token model still converts (and is not a unit model)", "gpt-x" in models and "gpt-x" not in unit_models)
os.makedirs(os.path.dirname(price_sync.CACHE), exist_ok=True)
json.dump({"_fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "models": models, "providers": provs, "unit_models": unit_models}, open(price_sync.CACHE, "w"))
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

print(f"\n{'[FAIL]' if failures else 'OK'} test_price_refresh: {failures} failure(s)")
sys.exit(1 if failures else 0)
