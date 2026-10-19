"""No double-usage — a subscription LANE subprocess must carry NO provider metered key, so it can only ride its
PLAN and can never make a metered API call. Above all, a NON-Claude lane (codex/gemini/zai) must never inherit
ANTHROPIC_API_KEY and silently spend Claude tokens for work meant to ride another plan. `config.lane_plan_env()` is
that structural guarantee; every subprocess lane builds its child env from it. Offline: pokes the scrubber and
captures the env the codex lane hands its subprocess — no real subprocess spawn, no LLM, no network.
"""
import os
import subprocess
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
from spendguard import codex_daemon, warm_stdio_daemon                                  # noqa: E402


captured = {}
_o_run = warm_stdio_daemon.subprocess.Popen
worker = codex_daemon._CodexDaemon()
worker._spawn_cmd = lambda: [sys.executable]  # verified local executable; Popen is intercepted below
try:
    def _cap_run(cmd, **kw):
        captured.update(kw)
        raise OSError()  # stop before protocol startup; no live process is created
    warm_stdio_daemon.subprocess.Popen = _cap_run
    worker._spawn()
    cenv = captured.get("env", {})
    fails += ck("warm subprocess boundary was reached", bool(cenv))
    fails += ck("the env actually passed to the codex subprocess has NO ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY" not in cenv)
    fails += ck("...and NO OPENAI_API_KEY (it rides the ChatGPT plan login, not the metered key)", "OPENAI_API_KEY" not in cenv)
    fails += ck("...yet still carries the non-key vars it needs", cenv.get("SG_NONKEY_MARKER") == "keepme")
    fails += ck("warm subprocess stdin is a private RPC pipe", captured.get("stdin") is subprocess.PIPE)
    fails += ck("warm subprocess uses its fixed safe cwd", captured.get("cwd") == warm_stdio_daemon._SAFE_CWD)
finally:
    warm_stdio_daemon.subprocess.Popen = _o_run

for k in list(_KEYS) + ["SG_NONKEY_MARKER"]:
    os.environ.pop(k, None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_no_double_usage: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
