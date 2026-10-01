"""STATIC .env shadow audit — the latent shadow `doctor` cannot see.

`doctor`/key_shadow_report read the RUNTIME os.environ, so a bare CLI never runs the app's load_dotenv() and cannot
see a repo `.env` holding an uncommented provider key that WOULD shadow the (possibly rotated) keys.env key at runtime
— the warden 401. config.dotenv_key_shadow_report + `spendguard keys-audit` read the FILES directly and flag it:
'differ' (dangerous) / 'dup' (redundant). Only a name keys.env ITSELF declares is considered — whether a var is a
provider key is keys.env's own declaration, never guessed from the name — so a commented / profile-suffixed / empty
line, AND any undeclared name (a provider-looking DEEPSEEK_API_KEY keys.env never set, or an app secret like
CSRF_TOKEN), never flags. Offline + hermetic: a temp keys.env + temp repo dirs under SPENDGUARD_HOME. Last-4 only —
the full secret must never appear in any output."""
import contextlib
import io
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-dotenv-audit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("SPENDGUARD_KEY_PROFILE", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config          # noqa: E402
from spendguard import keys_audit      # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# Full secrets (never printed) — the SSOT value vs a stale shadow; last-4 are the only thing any output may show.
_SSOT_OPENAI = "sk-proj-ssot00000000000000000000qjMA"      # keys.env (current) — last4 qjMA
_STALE_OPENAI = "sk-proj-stale0000000000000000000AyIA"     # a rotated-away key still sitting in a repo .env — last4 AyIA
_SSOT_ANTHROPIC = "sk-ant-ssot0000000000000000000000owAA"  # keys.env anthropic — last4 owAA
_EXTRA_DEEPSEEK = "sk-ds-000000000000000000000000ffff"     # a provider keys.env never declares — last4 ffff

with open(config.KEYS_ENV, "w") as f:
    f.write("OPENAI_API_KEY=%s\nANTHROPIC_API_KEY=%s\n" % (_SSOT_OPENAI, _SSOT_ANTHROPIC))


def _repo(name, env_text, env_local_text=None):
    """A temp repo dir holding a .env (and optionally a .env.local)."""
    d = tempfile.mkdtemp(prefix="repo-%s-" % name)
    with open(os.path.join(d, ".env"), "w") as f:
        f.write(env_text)
    if env_local_text is not None:
        with open(os.path.join(d, ".env.local"), "w") as f:
            f.write(env_local_text)
    return d


def _by_name(findings):
    return {f["name"]: f for f in findings}


print("-- DIFFER: a repo .env holds a DIFFERENT value than keys.env (the warden 401) --")
r = _repo("differ", "OPENAI_API_KEY=%s\n" % _STALE_OPENAI)
rep = config.dotenv_key_shadow_report([r])
d = _by_name(rep).get("OPENAI_API_KEY", {})
ck("one finding for OPENAI_API_KEY", len(rep) == 1 and "OPENAI_API_KEY" in _by_name(rep))
ck("state is 'differ'", d.get("state") == "differ")
ck("file4 = the stale key's last-4, declared4 = the SSOT's last-4", d.get("file4") == "AyIA" and d.get("declared4") == "qjMA")

print("-- DUP: a repo .env duplicates the SSOT value exactly --")
r = _repo("dup", "OPENAI_API_KEY=%s\n" % _SSOT_OPENAI)
d = _by_name(config.dotenv_key_shadow_report([r])).get("OPENAI_API_KEY", {})
ck("state is 'dup'", d.get("state") == "dup")
ck("dup carries declared4", d.get("declared4") == "qjMA")

print("-- UNDECLARED names are SKIPPED: provider-ness is keys.env's declaration, never a name guess --")
r = _repo("undeclared",
          "DEEPSEEK_API_KEY=%s\n"                              # provider-LOOKING, but keys.env never declares it → shadows nothing
          "CSRF_TOKEN=app-csrf-secret-not-a-provider-key\n" % _EXTRA_DEEPSEEK)  # app secret ending _TOKEN → must NOT read as a provider key
ck("a provider-looking key keys.env never declares is not flagged", config.dotenv_key_shadow_report([r]) == [])
ck("an app secret ending _TOKEN is NOT mistaken for a provider key (the enforcer's case)",
   all(f["name"] != "CSRF_TOKEN" for f in config.dotenv_key_shadow_report([r])))

print("-- SKIP: commented / profile-suffixed / empty never flag (even for a keys.env-declared name) --")
r = _repo("skips",
          "# OPENAI_API_KEY=%s  (moved to keys.env SSOT)\n"   # commented → parser skips
          "OPENAI_API_KEY__warden=%s\n"                        # a declared per-repo profile — not a bare shadow
          "ANTHROPIC_API_KEY=\n" % (_STALE_OPENAI, _STALE_OPENAI))  # empty value shadows nothing
ck("no findings from commented / profile / empty lines", config.dotenv_key_shadow_report([r]) == [])

print("-- directory scans .env AND .env.local; explicit-file + dir dedups --")
r = _repo("multi", "OPENAI_API_KEY=%s\n" % _STALE_OPENAI, env_local_text="ANTHROPIC_API_KEY=%s\n" % _STALE_OPENAI)
rep = config.dotenv_key_shadow_report([r])
ck("both .env and .env.local are scanned", {f["name"] for f in rep} == {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"})
rep2 = config.dotenv_key_shadow_report([r, os.path.join(r, ".env")])   # dir + explicit same file
ck("a file named via dir AND explicitly is audited once", len([f for f in rep2 if f["path"].endswith("/.env")]) == 1)

print("-- CLI exit codes: differ→1, clean→0, dup→0 but --strict→1 --")
r_differ = _repo("cli-differ", "OPENAI_API_KEY=%s\n" % _STALE_OPENAI)
r_clean = _repo("cli-clean", "# nothing but a comment\nDATABASE_URL=x\n")
r_dup = _repo("cli-dup", "OPENAI_API_KEY=%s\n" % _SSOT_OPENAI)
with contextlib.redirect_stdout(io.StringIO()):
    rc_differ = keys_audit.cmd([r_differ])
    rc_clean = keys_audit.cmd([r_clean])
    rc_dup = keys_audit.cmd([r_dup])
    rc_dup_strict = keys_audit.cmd([r_dup, "--strict"])
ck("differ → exit 1", rc_differ == 1)
ck("clean → exit 0", rc_clean == 0)
ck("dup → exit 0 (not dangerous)", rc_dup == 0)
ck("dup --strict → exit 1", rc_dup_strict == 1)

print("-- the FULL secret never appears in human or json output (last-4 only) --")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    keys_audit.cmd([r_differ])
    keys_audit.cmd([r_differ, "--json"])
out = buf.getvalue()
ck("no full OPENAI secret in output", _STALE_OPENAI not in out and _SSOT_OPENAI not in out)
ck("the last-4 IS present", "AyIA" in out)

print(f"\n{'[FAIL]' if fails else 'OK'} test_dotenv_key_audit: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
