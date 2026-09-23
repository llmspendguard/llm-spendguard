"""Guard — submit.submit_chat_tasks: the first-class chat Batch-API submitter (the chat analogue of
adapters.embed_batch) that wires bulk_delegate's / route_economics' BATCH leg to a real /v1/chat/completions
submission. Pins its contract hermetically (build_chat_batch_jsonl + guarded_submit + provider_for stubbed — no
network, no pricing, no OpenAI upload):
  · each task becomes one {custom_id, content} line — a STRING task gets custom_id 'task-<i>'; a DICT task keeps its
    own custom_id and its per-line system/schema/schema_name overrides — and THAT tasks file is what build receives;
  · OpenAI-only: a non-OpenAI model returns a TYPED error (no raise, no temp file written, build never called) — the
    lane fan runs it instead, never a silent metered fallback;
  · a plain downstream failure (build/guarded_submit raises a non-stop error) → an error dict, never a caller crash;
  · a DELIBERATE spend-stop from guarded_submit PROPAGATES (never swallowed into an error dict);
  · cap_dollars + endpoint + the submit flag reach guarded_submit; submit=False → estimate-only (batch_id None);
  · empty tasks → a $0 no-op ({requests:0}).
Plus ONE real-build contract check: the tasks file submit_chat_tasks writes is consumable by the REAL
build_chat_batch_jsonl and yields a valid Batch envelope with the custom_ids preserved verbatim (resolve_effort +
guarded_submit stubbed so it stays offline).

Isolation: SPENDGUARD_HOME/SPENDGUARD_TEST_ISOLATED are set at the top BEFORE spendguard is imported (the chunked
suite already sets them in the child env); no self-re-exec, so nothing is stubbed against a real home."""
import os
import sys
import json
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-chatbatch-"))

from spendguard import submit, adapters, models, gate

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_REAL_BUILD = submit.build_chat_batch_jsonl          # saved for the Test-B contract check (Test A stubs it)

# deterministic provider gate: gpt* → openai, anything else → not-openai (no catalog dependency)
adapters.provider_for = lambda m: "openai" if str(m).startswith("gpt") else "anthropic"

# ── Test A: submit_chat_tasks UNIT (build + guarded_submit stubbed; a build stub reads the written tasks file) ──
_seen = {}
def _stub_build_reading_tasks(tasks_path, model, system=None, max_out=None, reasoning="minimal",
                              schema=None, schema_name="result"):
    _seen["rows"] = [json.loads(ln) for ln in open(tasks_path) if ln.strip()]
    _seen["build_model"] = model
    _seen["build_schema"] = schema
    return "/tmp/spendguard-fake-envelope-%d.jsonl" % os.getpid(), len(_seen["rows"])
def _stub_guarded_capture(jsonl_path, model, cap_dollars, batch=True, submit=True,
                          endpoint="/v1/chat/completions", **kw):
    _seen["cap"] = cap_dollars
    _seen["endpoint"] = endpoint
    _seen["submit"] = submit
    return "batch_STUB" if submit else None
submit.build_chat_batch_jsonl = _stub_build_reading_tasks
submit.guarded_submit = _stub_guarded_capture

print("-- string tasks: custom_id 'task-<i>' + content round-trip; cap/endpoint/submit reach guarded_submit --")
_seen.clear()
r = submit.submit_chat_tasks(["alpha", "beta"], "gpt-4.1-nano", cap_dollars=3.5)
ck("returns the guarded batch_id", r["batch_id"] == "batch_STUB")
ck("requests == n tasks, no error", r["requests"] == 2 and r["error"] is None)
ck("two {custom_id, content} lines written", len(_seen.get("rows", [])) == 2)
ck("string task 0 → custom_id 'task-0', content 'alpha'",
   _seen["rows"][0] == {"custom_id": "task-0", "content": "alpha"})
ck("cap_dollars forwarded to guarded_submit", _seen["cap"] == 3.5)
ck("endpoint is /v1/chat/completions", _seen["endpoint"] == "/v1/chat/completions")
ck("submit=True forwarded", _seen["submit"] is True)

print("-- dict tasks: custom_id + per-line system/schema/schema_name overrides preserved into the tasks file --")
_seen.clear()
SCH = {"type": "object", "properties": {"x": {"type": "string"}}}
submit.submit_chat_tasks(
    [{"custom_id": "row-A", "content": "hi", "system": "be terse", "schema": SCH, "schema_name": "res"}],
    "gpt-4.1-nano")
ck("dict task keeps its custom_id verbatim", _seen["rows"][0]["custom_id"] == "row-A")
ck("per-line system override written", _seen["rows"][0]["system"] == "be terse")
ck("per-line schema override written", _seen["rows"][0]["schema"] == SCH)
ck("per-line schema_name written", _seen["rows"][0].get("schema_name") == "res")

print("-- OpenAI-only: a non-OpenAI model → typed error, NO raise, build never called --")
_seen.clear()
r3 = submit.submit_chat_tasks(["z"], "claude-opus-4-8")
ck("non-OpenAI → error set, batch_id None", r3["batch_id"] is None and bool(r3["error"]))
ck("error names the OpenAI-only boundary", "OpenAI-only" in (r3["error"] or ""))
ck("build never reached for a non-OpenAI model", "rows" not in _seen)

print("-- empty tasks → a $0 no-op, build never called --")
_seen.clear()
r4 = submit.submit_chat_tasks([], "gpt-4.1-nano")
ck("empty → requests 0, batch_id None, no error",
   r4 == {"batch_id": None, "jsonl": None, "requests": 0, "error": None})
ck("build never reached for empty tasks", "rows" not in _seen)

print("-- a plain downstream failure → error dict, never a crash --")
def _stub_build_raises(*a, **k):
    raise RuntimeError("build blew up")
submit.build_chat_batch_jsonl = _stub_build_raises
r5 = submit.submit_chat_tasks(["q"], "gpt-4.1-nano")
ck("build failure → error dict, batch_id None", r5["batch_id"] is None and "build blew up" in (r5["error"] or ""))
submit.build_chat_batch_jsonl = _stub_build_reading_tasks

print("-- a DELIBERATE spend-stop from guarded_submit PROPAGATES (never swallowed into an error dict) --")
def _stub_guarded_refuses(*a, **k):
    raise gate.SpendGateRefused.__new__(gate.SpendGateRefused)
submit.guarded_submit = _stub_guarded_refuses
try:
    submit.submit_chat_tasks(["q"], "gpt-4.1-nano")
    ck("deliberate stop propagated", False)
except gate.SpendGateRefused:
    ck("deliberate stop propagated (SpendGateRefused, not an error dict)", True)
submit.guarded_submit = _stub_guarded_capture

print("-- submit=False → estimate-only (batch_id None), the submit flag reaches guarded_submit --")
_seen.clear()
r6 = submit.submit_chat_tasks(["e"], "gpt-4.1-nano", submit=False)
ck("submit=False → batch_id None, no error", r6["batch_id"] is None and r6["error"] is None)
ck("submit=False forwarded to guarded_submit", _seen["submit"] is False)

# ── Test B: the REAL build_chat_batch_jsonl consumes the tasks file submit_chat_tasks writes (offline) ──
print("-- contract: REAL build turns the written tasks file into a valid Batch envelope, custom_ids verbatim --")
models.resolve_effort = lambda m, level: "minimal"           # offline — no discover_efforts network probe
_env = {}
def _stub_guarded_env_path(jsonl_path, model, cap_dollars, batch=True, submit=True,
                           endpoint="/v1/chat/completions", **kw):
    _env["path"] = jsonl_path                                 # capture the REAL built envelope, submit nothing
    return None
submit.build_chat_batch_jsonl = _REAL_BUILD
submit.guarded_submit = _stub_guarded_env_path
r7 = submit.submit_chat_tasks([{"custom_id": "x1", "content": "hello"},
                               {"custom_id": "x2", "content": "world"}], "gpt-4.1-nano", submit=False)
env_lines = [json.loads(ln) for ln in open(_env["path"]) if ln.strip()]
ck("real build produced 2 envelope lines", len(env_lines) == 2)
ck("custom_ids preserved verbatim [x1, x2]", [ln["custom_id"] for ln in env_lines] == ["x1", "x2"])
ck("each line targets /v1/chat/completions", all(ln["url"] == "/v1/chat/completions" for ln in env_lines))
ck("user content round-trips into the envelope body",
   [ln["body"]["messages"][-1]["content"] for ln in env_lines] == ["hello", "world"])
ck("result.jsonl is the built envelope; requests == 2", r7["jsonl"] == _env["path"] and r7["requests"] == 2)
try:
    os.unlink(_env["path"])
except OSError:
    pass

# ── a STRUCTURED batch with a SMALL max_out is FLOORED to TOKEN_FLOOR (the reasoning-model silent-truncation fix) ──
print("-- STRUCTURED batch: a small max_out is floored to TOKEN_FLOOR so the JSON can't silently truncate --")
_env2 = {}
def _stub_guarded_env2(jsonl_path, model, cap_dollars, batch=True, submit=True,
                       endpoint="/v1/chat/completions", **kw):
    _env2["path"] = jsonl_path
    return None
submit.guarded_submit = _stub_guarded_env2
SCH2 = {"type": "object", "properties": {"verdict": {"type": "string"}}}
submit.submit_chat_tasks([{"custom_id": "s1", "content": "classify this"}], "gpt-4.1-nano",
                         schema=SCH2, max_out=200, submit=False)
_sbody = [json.loads(ln) for ln in open(_env2["path"]) if ln.strip()][0]["body"]
_stok = _sbody.get("max_tokens") or _sbody.get("max_completion_tokens")
ck("a small max_out (200) on a STRUCTURED batch is floored to TOKEN_FLOOR, not honored",
   _stok == adapters.TOKEN_FLOOR)
try:
    os.unlink(_env2["path"])
except OSError:
    pass

print(f"\n{'[FAIL]' if _fails else 'OK'} test_submit_chat_tasks: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
