"""keys.env secrets file + gate.enforce / VAST_API_KEY schema — the 2-file config UX (config.json + keys.env).

Guards:
  • load_key_files() loads ~/.spendguard/keys.env into os.environ so the user's OWN openai/anthropic clients see
    the keys after a plain `import spendguard` (not just spendguard's internal lookups);
  • a REAL env var ALWAYS wins and blank placeholders are skipped — prod / CI / secret-managers never clobbered;
  • `spendguard init` scaffolds keys.env with one placeholder per secret (LLM + vast + org key), chmod 600, idempotent;
  • gate.enforce (off|warn|block) + VAST_API_KEY are in the documented schema, and gate.enforce actually drives bulkgate.
Offline, no network, zero spend."""
import os, sys, tempfile, io, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-keysenv-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# a real env var set BEFORE spendguard loads keys.env — must never be overridden by the file
os.environ["PRESET_REAL_KEY"] = "real-wins"
for k in ("FAKE_PROVIDER_KEY", "EXPORTED_KEY", "BLANK_PLACEHOLDER_KEY", "SPENDGUARD_ENFORCE"):
    os.environ.pop(k, None)

from spendguard import config, config_schema, setup, bulkgate

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── load_key_files: keys.env → os.environ, tolerant, real env wins, blanks skipped ──
config.KEYS_ENV.write_text(
    "# a comment line\n"
    "FAKE_PROVIDER_KEY=sk-fromfile\n"
    'export EXPORTED_KEY = "quoted-val"\n'
    "BLANK_PLACEHOLDER_KEY=\n"
    "PRESET_REAL_KEY=should-be-ignored\n"
)
config.load_key_files()
ck("keys.env value loaded into os.environ", os.environ.get("FAKE_PROVIDER_KEY") == "sk-fromfile")
ck("tolerant of `export ` prefix + quotes/spaces", os.environ.get("EXPORTED_KEY") == "quoted-val")
ck("blank placeholder is NOT set (won't clobber)", "BLANK_PLACEHOLDER_KEY" not in os.environ)
ck("a real env var WINS over keys.env", os.environ.get("PRESET_REAL_KEY") == "real-wins")

# ── scaffold: a placeholder per secret, chmod 600, idempotent ──
config.KEYS_ENV.unlink()
p, created = setup._scaffold_keys_env()
ck("_scaffold_keys_env creates the file", created and p.exists())
body = p.read_text()
for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
             "DASHSCOPE_API_KEY", "VAST_API_KEY", "SPENDGUARD_SAAS_KEY", "ZAI_API_KEY"):
    ck(f"scaffold has a {name}= placeholder", (name + "=") in body)
ck("scaffold placeholders are BLANK (no leaked values)", "sk-" not in body)
ck("scaffold is idempotent (won't clobber an existing file)", setup._scaffold_keys_env()[1] is False)
ck("keys.env is chmod 600", (os.stat(p).st_mode & 0o777) == 0o600)

# ── gate.enforce enum in the schema + it drives bulkgate; VAST_API_KEY present ──
enf = [s for s in config_schema.SETTINGS if s["section"] == "gate" and s["key"] == "enforce"]
ck("gate.enforce is in the schema", len(enf) == 1)
ck("gate.enforce enum = off,warn,block", bool(enf) and enf[0]["kind"] == "enum:off,warn,block")
ck("gate.enforce default = warn", bool(enf) and enf[0]["default"] == "warn")
ck("VAST_API_KEY is in the schema", any(s["key"] == "VAST_API_KEY" for s in config_schema.SETTINGS))

# ── z.ai / GLM provider wired: key in schema (→ keys.env), routes glm- models, priced (stub) ──
from spendguard import adapters, pricing
ck("ZAI_API_KEY is in the schema", any(s["key"] == "ZAI_API_KEY" for s in config_schema.SETTINGS))
ck("adapters routes a glm- model to the zai provider", adapters.provider_for("glm-5.2") == "zai")
ck("zai provider uses ZAI_API_KEY", adapters.PROVIDERS["zai"]["key_env"] == "ZAI_API_KEY")
try:
    _pr = pricing.price("glm-5.2")
    ck("glm-5.2 resolves a (stub) price", bool(_pr) and float(_pr.get("in_") or 0) > 0)
except Exception:
    ck("glm-5.2 resolves a (stub) price", False)

config.CONFIG_JSON.write_text('{"gate": {"enforce": "block"}}')
config._cfg._cache = None
ck("config gate.enforce=block drives bulkgate.mode()", bulkgate.mode() == "block")

# ── init --quick scaffolds BOTH files, no prompts, offline ──
config.CONFIG_JSON.unlink()
config.KEYS_ENV.unlink()
config._cfg._cache = None
with contextlib.redirect_stdout(io.StringIO()):
    rc = setup.cmd_init(["--quick", "--local"])
ck("init --quick returns 0", rc == 0)
ck("init --quick wrote config.json", config.CONFIG_JSON.exists())
ck("init --quick scaffolded keys.env", config.KEYS_ENV.exists())

print(("[OK]" if not fails else "[FAIL]") + " keys-env/config-ux: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
