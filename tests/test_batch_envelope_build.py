"""batch-submit is TASK-based: the caller supplies {custom_id, content} + a shared system + a model, and spendguard
BUILDS the per-model /v1/chat/completions envelope via submit.build_chat_batch_jsonl → models.apply_call_params (the
ONE authority for the token param, max_tokens vs max_completion_tokens, and reasoning).

This guards the failure that returned 250/250 HTTP 400 ("'max_tokens' is not supported with this model; use
'max_completion_tokens'"): a caller task can NEVER again reach OpenAI with a model-wrong param. Also pinned:
custom_id preserved VERBATIM, a reasoning model gets a HEADROOM output ceiling (no empty-from-reasoning — the
max_output_poisoning trap), system sent as a system message, and a non-OpenAI model / malformed task refused
fail-closed. Offline: builds the envelope only, makes NO API call.
"""
import os
import sys
import json
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-batch-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import submit, adapters   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []


def _write_tasks(rows):
    fd, p = tempfile.mkstemp(prefix="sg-tasks-", suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _read_built(path):
    with open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


TASKS = [{"custom_id": "row-1", "content": "describe def foo(): ..."},
         {"custom_id": "row-2", "content": "describe class Bar: ..."}]
tasks_p = _write_tasks(TASKS)

print("-- a gpt-5.x model → max_completion_tokens + reasoning_effort (NEVER max_tokens); custom_id verbatim --")
built5, n5 = submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", system="You are terse.")
rows5 = _read_built(built5)
os.unlink(built5)
fails += report_check("built one request per task", n5 == 2 and len(rows5) == 2)
fails += report_check("custom_id preserved VERBATIM (the caller's mapping key)",
                      [r["custom_id"] for r in rows5] == ["row-1", "row-2"])
fails += report_check("every body uses max_completion_tokens, NEVER max_tokens (the 400 that failed 250/250)",
                      all("max_completion_tokens" in r["body"] and "max_tokens" not in r["body"] for r in rows5))
fails += report_check("reasoning_effort SET for the reasoning model (else it returns EMPTY)",
                      all(r["body"].get("reasoning_effort") for r in rows5))
fails += report_check("output ceiling has HEADROOM (>= TOKEN_FLOOR) so reasoning can't empty the reply",
                      all(int(r["body"]["max_completion_tokens"]) >= adapters.TOKEN_FLOOR for r in rows5))
fails += report_check("system is a system message + the user content is preserved",
                      all(r["body"]["messages"][0] == {"role": "system", "content": "You are terse."}
                          and r["body"]["messages"][-1]["role"] == "user" for r in rows5))
fails += report_check("each line is a batch chat-completions envelope (method POST, url /v1/chat/completions)",
                      all(r["url"] == "/v1/chat/completions" and r["method"] == "POST" for r in rows5))

print("\n-- an OLDER gpt- model → max_tokens (NEVER max_completion_tokens): the family split from models.py --")
built4, _ = submit.build_chat_batch_jsonl(tasks_p, "gpt-4o-mini", system="You are terse.")
rows4 = _read_built(built4)
os.unlink(built4)
fails += report_check("every body uses max_tokens, NEVER max_completion_tokens (older gpt- family)",
                      all("max_tokens" in r["body"] and "max_completion_tokens" not in r["body"] for r in rows4))
fails += report_check("older gpt- ALSO gets the TOKEN_FLOOR start-high ceiling (batch/realtime don't drift)",
                      all(int(r["body"]["max_tokens"]) >= adapters.TOKEN_FLOOR for r in rows4))

print("\n-- fail-closed: a non-OpenAI model and a malformed task are REFUSED, never a silent drop --")
_refused_model = False
try:
    submit.build_chat_batch_jsonl(tasks_p, "anthropic:claude-opus-4-8")
except ValueError:
    _refused_model = True
fails += report_check("a non-OpenAI model is refused (the OpenAI Batch API serves only OpenAI ids)", _refused_model)

bad_p = _write_tasks([{"content": "no custom_id here"}])
_refused_task = False
try:
    submit.build_chat_batch_jsonl(bad_p, "gpt-5.6-luna")
except ValueError:
    _refused_task = True
os.unlink(bad_p)
fails += report_check("a task missing custom_id/content is refused (never silently dropped)", _refused_task)

os.unlink(tasks_p)
print(f"\n{'[FAIL]' if fails else 'OK'} test_batch_envelope_build: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
