"""Key PROFILES + key FINGERPRINT — per-repo key selection and per-key spend attribution.

Profiles: one global keys.env holds every workspace/project-scoped key as `<VAR>__<profile>` entries;
a repo's `key_profile` (or $SPENDGUARD_KEY_PROFILE) selects them. Precedence: real env → profile entry
→ unsuffixed entry; suffixed entries NEVER leak without their profile. Fingerprint: budget.record
stamps sha256[:8]:last4 of the env-resolved provider key on every charge (local-only); by_key rolls up
$ per (provider, key); reconcile/true-down marker rows carry no key. Offline, zero network.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-keys-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# Seed keys.env into the ACTIVE isolated home BEFORE spendguard imports (load_key_files runs at config
# import). Outside the re-exec block on purpose: the pytest runner pre-sets SPENDGUARD_TEST_ISOLATED
# with its own home, so the block above is skipped there and seeding must happen either way.
_home = os.environ["SPENDGUARD_HOME"]
os.makedirs(_home, exist_ok=True)
open(os.path.join(_home, "keys.env"), "w").write(
    "OPENAI_API_KEY=sk-base-oai-0001\n"
    "OPENAI_API_KEY__lmm=sk-lmm-oai-1111\n"
    "ANTHROPIC_API_KEY__lmm=sk-lmm-ant-2222\n"
    "GEMINI_API_KEY=g-base-3333\n"
)
for _v in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    os.environ.pop(_v, None)      # a leaked real/runner env key must not shadow the seeded file

import hashlib
from spendguard import config, budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def reset_keys(profile=None, real_env=None):
    """Re-run the import-time loader under a chosen profile / pre-set real env."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "SPENDGUARD_KEY_PROFILE"):
        os.environ.pop(var, None)
    config._KEYS_SET_BY_SPENDGUARD.clear()
    if profile:
        os.environ["SPENDGUARD_KEY_PROFILE"] = profile
    for k, v in (real_env or {}).items():
        os.environ[k] = v
    config.load_key_files()


print("-- no profile: unsuffixed entries load; suffixed entries NEVER leak --")
reset_keys()
ck("base key loads", os.environ.get("OPENAI_API_KEY") == "sk-base-oai-0001")
ck("suffixed entry does not leak without its profile", "ANTHROPIC_API_KEY" not in os.environ)

print("-- profile active: profile entries override file base; base stands where no profile entry exists --")
reset_keys(profile="lmm")
ck("profile overrides the unsuffixed file entry", os.environ.get("OPENAI_API_KEY") == "sk-lmm-oai-1111")
ck("profile supplies vars the base lacks", os.environ.get("ANTHROPIC_API_KEY") == "sk-lmm-ant-2222")
ck("vars with no profile entry keep the base value", os.environ.get("GEMINI_API_KEY") == "g-base-3333")

print("-- a REAL environment variable always wins, even over the profile --")
reset_keys(profile="lmm", real_env={"OPENAI_API_KEY": "sk-real-from-ci"})
ck("real env beats the profile entry", os.environ.get("OPENAI_API_KEY") == "sk-real-from-ci")
ck("profile still fills the vars real env didn't set", os.environ.get("ANTHROPIC_API_KEY") == "sk-lmm-ant-2222")

print("-- key fingerprint: sha256[:8]:last4 of the serving key; '' when unknown --")
reset_keys(profile="lmm")
key = os.environ["ANTHROPIC_API_KEY"]
want = hashlib.sha256(key.encode()).hexdigest()[:8] + ":" + key[-4:]
ck("fingerprint shape + value", config.key_fingerprint("anthropic") == want)
ck("unknown provider → ''", config.key_fingerprint("vast.ai") == "")
os.environ.pop("ANTHROPIC_API_KEY")
ck("no key set → ''", config.key_fingerprint("anthropic") == "")
os.environ["ANTHROPIC_API_KEY"] = key

print("-- charges stamp the key; by_key rolls up per (provider, key); marker rows carry none --")
budget.record("anthropic", "claude-haiku-4-5", "batch", 12.5, project="lmm")
budget.record("anthropic", "claude-haiku-4-5", "realtime", 2.5, project="lmm")
os.environ["ANTHROPIC_API_KEY"] = "sk-second-key-9999"      # a second key serves the next call
budget.record("anthropic", "claude-opus-4-8", "batch", 5.0, project="healiom")
budget.record_reconciled("2026-06-03", "anthropic", 7.0, project="lmm")           # mirror row: no key
budget.record_true_down("2026-06-03", "anthropic", "claude-haiku-4-5", 1.0, "lmm")  # correction row: no key
bk = budget.by_key()
fp2 = hashlib.sha256(b"sk-second-key-9999").hexdigest()[:8] + ":9999"
ck("first key carries batch+realtime ($15)", abs(bk[("anthropic", want)]["cost"] - 15.0) < 1e-6
   and bk[("anthropic", want)]["calls"] == 2)
ck("second key attributed separately ($5)", abs(bk[("anthropic", fp2)]["cost"] - 5.0) < 1e-6)
ck("reconcile/true-down marker rows excluded from the per-key view",
   ("anthropic", "(none)") not in bk)
row = budget._db().execute("SELECT key_fp FROM charges WHERE model=? AND kind='batch'",
                           ("claude-opus-4-8",)).fetchone()
ck("the stamped fp is the key at RECORD time (not a cached stale one)", row and row[0] == fp2)

print("-- schema: the new knobs are documented --")
from spendguard import config_schema
s = {(o["section"], o["key"]): o for o in config_schema.SETTINGS}
ck("keys.key_profile documented", ("keys", "key_profile") in s
   and s[("keys", "key_profile")]["env"] == "SPENDGUARD_KEY_PROFILE")
ck("advisor.pool_cooldown_s documented", ("advisor", "pool_cooldown_s") in s)
ck("advisor.executor enum covers codex + pool",
   "codex" in s[("advisor", "executor")]["kind"] and "pool" in s[("advisor", "executor")]["kind"])

print(f"\n{'[FAIL]' if fails else 'OK'} test_key_profiles: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
