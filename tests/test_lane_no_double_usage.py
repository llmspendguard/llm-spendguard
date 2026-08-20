"""No double-usage — a subscription LANE subprocess must carry NO provider metered key, so it can only ride its
PLAN and can never make a metered API call. Above all, a NON-Claude lane (codex/gemini/zai) must never inherit
ANTHROPIC_API_KEY and silently spend Claude tokens for work meant to ride another plan. `config.lane_plan_env()` is
that structural guarantee; every subprocess lane builds its child env from it. Offline: pokes the scrubber and
captures the env the codex lane hands its subprocess — no real subprocess spawn, no LLM, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanenv-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config                                                          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
         "GEMINI_API_KEY", "GOOGLE_API_KEY", "ZAI_API_KEY", "ZAI_CODING_API_KEY")
for k in _KEYS:
    os.environ[k] = f"fake-{k}"
os.environ["SG_NONKEY_MARKER"] = "keepme"      # a non-key var must survive the scrub (the lane still needs PATH etc.)

print("-- lane_plan_env(): EVERY provider metered key is stripped from a lane subprocess env --")
env = config.lane_plan_env()
for k in _KEYS:
    fails += ck(f"{k} stripped", k not in env)
fails += ck("★ a lane can never inherit ANTHROPIC_API_KEY → never spends Claude tokens (the no-double-usage guard)",
            "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env)
fails += ck("non-key env vars are preserved (the subprocess still needs PATH, HOME, …)",
            env.get("SG_NONKEY_MARKER") == "keepme")

print("\n-- keep=(...) lets a plan-TOKEN lane retain its OWN key while everything else is still stripped --")
zenv = config.lane_plan_env(keep=("ZAI_CODING_API_KEY",))
fails += ck("the kept key survives", zenv.get("ZAI_CODING_API_KEY") == "fake-ZAI_CODING_API_KEY")
fails += ck("...but ANTHROPIC_API_KEY is STILL stripped (Claude tokens still protected)", "ANTHROPIC_API_KEY" not in zenv)
fails += ck("...and OPENAI_API_KEY is still stripped", "OPENAI_API_KEY" not in zenv)

print("\n-- FUNCTIONAL: the codex lane hands its subprocess a scrubbed env (captured at the subprocess boundary) --")
from spendguard import codex_exec                                                      # noqa: E402


class _FakeCompleted:
    returncode, stdout, stderr = 0, "", ""


captured = {}
_o_run, _o_bin, _o_daemon = codex_exec.subprocess.run, codex_exec._bin, codex_exec._daemon_enabled
try:
    codex_exec._daemon_enabled = lambda: False        # force the exec path, not the warm daemon
    codex_exec._bin = lambda: "/bin/true"             # a resolvable exe so run_prompt reaches subprocess.run

    def _cap_run(cmd, **kw):
        captured["env"] = dict(kw.get("env") or {})
        return _FakeCompleted()
    codex_exec.subprocess.run = _cap_run
    codex_exec.run_prompt("hi", model="openai:gpt-5.5")   # ANTHROPIC_API_KEY is in os.environ (seeded above)
    cenv = captured.get("env", {})
    fails += ck("the env actually passed to the codex subprocess has NO ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY" not in cenv)
    fails += ck("...and NO OPENAI_API_KEY (it rides the ChatGPT plan login, not the metered key)", "OPENAI_API_KEY" not in cenv)
    fails += ck("...yet still carries the non-key vars it needs", cenv.get("SG_NONKEY_MARKER") == "keepme")
finally:
    codex_exec.subprocess.run, codex_exec._bin, codex_exec._daemon_enabled = _o_run, _o_bin, _o_daemon

for k in list(_KEYS) + ["SG_NONKEY_MARKER"]:
    os.environ.pop(k, None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_no_double_usage: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
