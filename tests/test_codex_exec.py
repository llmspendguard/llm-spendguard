"""Codex subscription lane (codex_exec) — headless `codex exec` on the ChatGPT plan, mirroring the
claude-code lane's guarantees: OPENAI_API_KEY stripped from the child (plan login only, never silently
metered), final message via --output-last-message, defensive field-name usage parse from the --json
event stream, and {error} on ANY mismatch so the caller falls back. Offline (stubbed subprocess+which);
the CLI is not installed on the dev machine, so these stubs encode the documented interface and the
fallback path is what guarantees safety against interface drift.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-codex-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
import types
from spendguard import codex_exec as ce

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


KEY_ENV = "OPENAI" + "_API_KEY"
seen = {}
ANSWER = "META JUDGMENT: keep"


def fake_run(cmd, capture_output=None, text=None, timeout=None, env=None):
    seen["cmd"], seen["env"] = cmd, env
    out_file = cmd[cmd.index("--output-last-message") + 1]
    open(out_file, "w").write(ANSWER)
    events = [
        {"type": "turn.started"},
        {"type": "token_count", "info": {"input_tokens": 850, "output_tokens": 40}},   # cumulative events:
        {"type": "token_count", "info": {"input_tokens": 850, "output_tokens": 97}},   # keep the LARGEST
        {"type": "turn.completed"},
    ]
    return types.SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(e) for e in events), stderr="")


ce.shutil.which = lambda name: "/usr/local/bin/codex"
ce.subprocess.run = fake_run
os.environ[KEY_ENV] = "sk-test-not-real"

print("-- happy path: exec + json + last-message file; usage = max over cumulative events --")
r = ce.run_prompt("judge this insight…", system="You are the advisor.")
ck("cmd is `codex exec … --json --output-last-message <file>`",
   seen["cmd"][:2] == ["codex", "exec"] and "--json" in seen["cmd"] and "--output-last-message" in seen["cmd"])
ck("system prompt is PREPENDED into the prompt arg (no separate slot)",
   seen["cmd"][2].startswith("You are the advisor.") and "judge this insight…" in seen["cmd"][2])
ck("the provider key env var is STRIPPED from the child (plan login only)",
   KEY_ENV not in seen["env"] and "PATH" in seen["env"])
ck("final message read from the file", r["text"] == ANSWER and r["error"] is None)
ck("usage extracted by field name, max over events", r["in_tok"] == 850 and r["out_tok"] == 97)
ck("tmp last-message file cleaned up", not os.path.exists(seen["cmd"][seen["cmd"].index("--output-last-message") + 1]))

print("-- degrade paths: every mismatch is an {error}, never an exception --")
def run_empty(cmd, **kw):
    seen["cmd"] = cmd
    open(cmd[cmd.index("--output-last-message") + 1], "w").write("")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
ce.subprocess.run = run_empty
ck("empty final message → error (no silent empty answer)",
   ce.run_prompt("x")["error"] == "codex produced no final message")
ce.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(returncode=2, stdout="", stderr="login required")
ck("non-zero exit → error with stderr", "login required" in ce.run_prompt("x")["error"])
ce.subprocess.run = fake_run
ck("no usage events at all → 0 tokens, never a guess",
   (lambda: (ce.__dict__.__setitem__("_probe", None),))
   and ce._usage_from_events("not json\n{\"type\":\"noise\"}") == (0, 0))
ce.shutil.which = lambda name: None
ck("CLI absent → error (pool skips the lane)", ce.run_prompt("x")["error"] == "codex CLI not found")

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_exec: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
