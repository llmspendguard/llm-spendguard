"""Key-source visibility + DRIFT ALARM — a repo's own .env / a shell export that SHADOWS keys.env is made LOUD.

The warden bug: lmm/.env held the OLD OpenAI key after keys.env was rotated; a real env var wins in api_key(), so
warden 401'd on an INVISIBLE stale shadow. This surfaces it: config.key_shadow_report / provider_key_status flag an
external value that DIFFERS from keys.env, while a DECLARED per-repo profile (`<VAR>__<profile>`, the sanctioned way
to run a repo on a different key) is NOT a shadow. Offline + hermetic: a temp keys.env under SPENDGUARD_HOME."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-keys-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("OPENAI_API_KEY", None)           # don't let an inherited real key skew the test
os.environ.pop("SPENDGUARD_KEY_PROFILE", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_NEW = "sk-proj-new0000000000000000000000qjMA"     # keys.env (current, valid) — last4 qjMA
_OLD = "sk-proj-old0000000000000000000000AyIA"     # a stale shadow — last4 AyIA
_PROF = "sk-proj-prof000000000000000000000zzzz"    # a declared per-repo profile key


def _write_keys(text):
    with open(config.KEYS_ENV, "w") as f:
        f.write(text)


print("-- no shadow: os.environ matches keys.env --")
_write_keys("OPENAI_API_KEY=%s\n" % _NEW)
os.environ["OPENAI_API_KEY"] = _NEW
config._KEYS_SET_BY_SPENDGUARD.discard("OPENAI_API_KEY")
ck("matching value → no shadow", config.key_shadow_report() == [])
ck("key_source is keys.env", config.key_source("OPENAI_API_KEY") == "keys.env")

print("-- SHADOW: an external env value differs from keys.env (the warden bug) --")
os.environ["OPENAI_API_KEY"] = _OLD              # a repo .env / shell export put the OLD key here
config._KEYS_SET_BY_SPENDGUARD.discard("OPENAI_API_KEY")   # not set by spendguard → external
rep = {s["name"]: s for s in config.key_shadow_report()}
ck("the external override is flagged as a shadow", "OPENAI_API_KEY" in rep)
ck("active4 = the OLD (wrong) key, declared4 = keys.env's NEW key",
   rep.get("OPENAI_API_KEY", {}).get("active4") == "AyIA" and rep["OPENAI_API_KEY"]["declared4"] == "qjMA")
ck("key_source is 'external'", config.key_source("OPENAI_API_KEY") == "external")
st = {s["prov"]: s for s in config.provider_key_status()}
ck("provider_key_status marks openai 'shadowed'", st["openai"]["state"] == "shadowed")

print("-- a DECLARED profile override is NOT a shadow (the sanctioned per-repo key) --")
_write_keys("OPENAI_API_KEY=%s\nOPENAI_API_KEY__warden=%s\n" % (_NEW, _PROF))
os.environ["SPENDGUARD_KEY_PROFILE"] = "warden"
os.environ["OPENAI_API_KEY"] = _PROF             # the profile value is active
config._KEYS_SET_BY_SPENDGUARD.discard("OPENAI_API_KEY")
ck("a declared profile override is NOT flagged as a shadow", config.key_shadow_report() == [])
ck("key_source names the profile", config.key_source("OPENAI_API_KEY") == "profile:warden")
os.environ.pop("SPENDGUARD_KEY_PROFILE", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_key_shadow: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
