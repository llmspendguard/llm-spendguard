"""Subscription executor (advisor.executor=claude-code) — spendguard's own meta prompts ride the
flat-fee plan: one-shot headless `claude -p` (no agent loop), the provider key env var STRIPPED from
the child (a plan call can never silently become metered API), $0 recorded on the billed axis, and any
failure FALLS BACK to the caged API path. Offline (stubbed subprocess + which), zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-subexec-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
import types
from spendguard import subscription_exec as se
from spendguard import adapters, calls

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


KEY_ENV = "ANTHROPIC" + "_API_KEY"                     # the env var the child must never see
seen = {}


def fake_run(cmd, capture_output=None, text=None, timeout=None, env=None):
    seen["cmd"], seen["env"] = cmd, env
    out = json.dumps({"type": "result", "is_error": False, "result": "SYNTHESIZED INSIGHT",
                      "usage": {"input_tokens": 900, "output_tokens": 120}})
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


se.shutil.which = lambda name: "/usr/local/bin/claude"
se.subprocess.run = fake_run
os.environ[KEY_ENV] = "sk-test-not-real"

r = se.run_prompt("evidence table…", system="You are the advisor.")
ck("headless one-shot: -p + json output + --max-turns 1 (no agent loop)",
   seen["cmd"][:2] == ["claude", "-p"] and "--max-turns" in seen["cmd"] and "json" in seen["cmd"])
ck("no model requested → no --model flag (CLI default)", "--model" not in seen["cmd"])
ck("system prompt rides --append-system-prompt", "--append-system-prompt" in seen["cmd"])
ck("the provider key env var is STRIPPED from the child (plan login only, never metered)",
   KEY_ENV not in seen["env"] and "PATH" in seen["env"])
ck("result + usage parsed", r["text"] == "SYNTHESIZED INSIGHT" and r["in_tok"] == 900 and r["out_tok"] == 120
   and r["error"] is None)

print("-- plan-window smartness: the requested tier is honored, never upgraded to the default model --")
se.run_prompt("cheap classify…", model="claude-haiku-4-5-20251001")
ck("haiku-class request → --model haiku",
   "--model" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--model") + 1] == "haiku")
se.run_prompt("judge…", model="claude-opus-4-8")
ck("opus-class request → --model opus", seen["cmd"][seen["cmd"].index("--model") + 1] == "opus")
se.run_prompt("mystery…", model="some-future-model-9")
ck("unknown family → no --model (degrade to CLI default, never error)", "--model" not in seen["cmd"])

# failure shapes → {error}, never raises
se.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="rate limited by plan")
ck("non-zero exit → error (caller falls back)", "rate limited" in se.run_prompt("x")["error"])
se.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="not json", stderr="")
ck("unparseable output → error", se.run_prompt("x")["error"] is not None)
se.shutil.which = lambda name: None
ck("CLI absent → error", "not found" in se.run_prompt("x")["error"])
se.shutil.which = lambda name: "/usr/local/bin/claude"

# ── adapters.call routing: plan first, corpus row at $0 billed, API fallback on error ──
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "claude-code"
se.subprocess.run = fake_run
recorded = []
calls.record = lambda provider, model, kind, cost, **k: recorded.append((provider, model, kind, cost))
out = adapters.call("claude-opus-4-8", "prompt", max_tokens=400, system="sys")
ck("adapters.call routes to the plan: $0 billed + executor tagged",
   out["cost"] == 0.0 and out["executor"] == "claude-code" and out["text"] == "SYNTHESIZED INSIGHT")
ck("corpus row recorded as kind='subscription' at $0 (billed axis honest)",
   recorded and recorded[-1] == ("anthropic", "claude-opus-4-8", "subscription", 0.0))

# executor fails ("plan window exhausted") → the call must FALL THROUGH to the API path. Under the
# suite's dead proxy the API attempt dies with ITS OWN error (connection/no-key) — either proves the
# fall-through happened: the executor's error is never what the caller sees.
se.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="plan window exhausted")
out2 = adapters.call("claude-opus-4-8", "prompt")
ck("executor failure FALLS BACK to the API path (the API path's own error surfaces, never the executor's)",
   out2.get("error") and "plan window" not in out2["error"] and out2.get("executor") is None)

os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
n0 = len(recorded)
out3 = adapters.call("claude-opus-4-8", "prompt")
ck("executor=api never touches the plan path (no subscription row, no executor tag)",
   out3.get("executor") is None and len(recorded) == n0)

print(("[OK]" if not fails else "[FAIL]") + " subscription executor: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
