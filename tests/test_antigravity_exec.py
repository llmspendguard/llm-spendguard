"""Antigravity (Gemini) subscription lane (antigravity_exec) — headless `agy -p … --output-format json` on the
Google AI plan, mirroring the codex/claude-code lanes: GEMINI_API_KEY + GOOGLE_API_KEY stripped from the child
(plan login only, never silently metered), text from the single json result's `response`, usage read by field
name, and {error} on ANY mismatch so the caller falls back. Offline (stubbed subprocess + which); the CLI need
not be installed — these stubs encode agy's verified `--output-format json` contract, and the fallback path is
what guarantees safety against interface drift.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-agy-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
import types
import shutil     # patch shutil.which directly — the shared config.resolve_cli() uses it (antigravity_exec
#                  delegates the PATH lookup to config.resolve_cli, exactly like codex_exec)
from spendguard import antigravity_exec as ax

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


GEMINI_KEY = "GEMINI" + "_API_KEY"
GOOGLE_KEY = "GOOGLE" + "_API_KEY"
seen = {}
RESULT = {"conversation_id": "c1", "status": "SUCCESS", "response": "OK\n", "duration_seconds": 1.5,
          "num_turns": 1, "usage": {"input_tokens": 850, "output_tokens": 40, "thinking_tokens": 5,
                                    "cache_read_tokens": 0, "total_tokens": 895}}


def fake_run(cmd, capture_output=None, text=None, timeout=None, env=None):
    seen["cmd"], seen["env"] = cmd, env
    return types.SimpleNamespace(returncode=0, stdout=json.dumps(RESULT), stderr="")


shutil.which = lambda name: "/usr/local/bin/agy" if name == "agy" else None
ax.subprocess.run = fake_run
os.environ[GEMINI_KEY] = "AIza-test-not-real"
os.environ[GOOGLE_KEY] = "gk-test-not-real"

print("-- happy path: agy -p … --output-format json; text from `response`, usage by field name --")
r = ax.run_prompt("judge this…", system="You are the advisor.", model="gemini:gemini-3.7-flash-medium")
ck("cmd is `agy -p <prompt> --output-format json --disable-slash-commands`",
   seen["cmd"][0].endswith("agy") and seen["cmd"][1] == "-p" and "--output-format" in seen["cmd"]
   and "json" in seen["cmd"] and "--disable-slash-commands" in seen["cmd"])
ck("system is PREPENDED into the prompt arg (print mode has no separate system slot)",
   seen["cmd"][2].startswith("You are the advisor.") and "judge this…" in seen["cmd"][2])
ck("requested model forwarded to --model, provider prefix stripped",
   "--model" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--model") + 1] == "gemini-3.7-flash-medium")
ck("GEMINI_API_KEY and GOOGLE_API_KEY are STRIPPED from the child (plan login only)",
   GEMINI_KEY not in seen["env"] and GOOGLE_KEY not in seen["env"] and "PATH" in seen["env"])
ck("text is the result `response`, stripped; error None", r["text"] == "OK" and r["error"] is None)
ck("usage extracted by field name", r["in_tok"] == 850 and r["out_tok"] == 40)

print("-- degrade paths: every mismatch is an {error}, never an exception, never a wrong answer --")
ax.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
    returncode=0, stdout=json.dumps(dict(RESULT, status="ERROR", response="", error="quota exhausted")), stderr="")
ck("status != SUCCESS → error (carries agy's error message)", "quota exhausted" in (ax.run_prompt("x")["error"] or ""))
ax.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=2, stdout="", stderr="agy: auth required")
ck("non-zero exit → error with stderr", "auth required" in (ax.run_prompt("x")["error"] or ""))
ax.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps(dict(RESULT, response="  ")), stderr="")
ck("SUCCESS but empty response → error (no silent empty answer)", ax.run_prompt("x")["error"] == "agy produced no response text")
ax.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="not json\nnoise", stderr="")
ck("unparseable stdout → error (degrades to API)", "no parseable json" in (ax.run_prompt("x")["error"] or ""))
ax.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps(dict(RESULT, usage={})), stderr="")
_r = ax.run_prompt("x")
ck("absent usage → 0 tokens, never a guess", _r["in_tok"] == 0 and _r["out_tok"] == 0 and _r["error"] is None)

shutil.which = lambda name: None
os.environ["SPENDGUARD_AGY_BIN"] = "/nonexistent/agy"     # a missing pin fails LOUD (see the codex twin test)
ck("CLI absent → error (pool skips the lane)", ax.run_prompt("x")["error"] == "agy (Antigravity CLI) not found")
del os.environ["SPENDGUARD_AGY_BIN"]

print(f"\n{'[FAIL]' if fails else 'OK'} test_antigravity_exec: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
