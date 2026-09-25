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

from spendguard import submit, adapters, models   # noqa: E402


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
_ob4 = adapters.output_budget("gpt-4o-mini")   # the ONE canonical send-budget — batch must equal it (no drift)
fails += report_check("older gpt- gets the model's real start-high ceiling, IDENTICAL batch and realtime (no drift) — "
                      "gpt-4o-mini's is 16384, its published max, honestly BELOW the 32K floor (a published max may be lower)",
                      all(int(r["body"]["max_tokens"]) == _ob4 for r in rows4))

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

print("\n-- reasoning is resolved via models.resolve_effort (the batch-safe ACCEPTED value), not the raw family --")
# A batch can't heal per row, so the builder must send a VERIFIABLY-accepted reasoning_effort. gpt-5.6-luna rejects
# the family 'minimal' and accepts 'none'; the builder must use resolve_effort's value, and OMIT when it returns None.
_orig_resolve = models.resolve_effort
try:
    models.resolve_effort = lambda _m, _lvl: "none"     # simulate: endpoint accepts 'none', not the family 'minimal'
    _bp, _ = submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", system="t")
    _rows = _read_built(_bp)
    os.unlink(_bp)
    fails += report_check("body reasoning_effort == resolve_effort's accepted value ('none'), NOT the family 'minimal'",
                          all(r["body"].get("reasoning_effort") == "none" for r in _rows))
    models.resolve_effort = lambda _m, _lvl: None        # simulate: no accepted effort → OMIT the param
    _bp2, _ = submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", system="t")
    _rows2 = _read_built(_bp2)
    os.unlink(_bp2)
    fails += report_check("resolve_effort None → reasoning_effort OMITTED (model default, not a rejected value)",
                          all("reasoning_effort" not in r["body"] for r in _rows2))
finally:
    models.resolve_effort = _orig_resolve

print("\n-- structured output: `schema` binds the vendor's STRICT response_format on every line (the realtime binding) --")
SCHEMA = {"type": "object", "properties": {"results": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "label": {"type": "string", "enum": ["a", "b"]}}, "required": ["id"]}}},
    "required": ["results"]}
_bs, _ = submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", system="t", schema=SCHEMA, schema_name="cards")
_rs = _read_built(_bs)
os.unlink(_bs)
fails += report_check("every body carries response_format.json_schema with strict=True and the caller's name",
                      all(r["body"].get("response_format", {}).get("type") == "json_schema"
                          and r["body"]["response_format"]["json_schema"]["strict"] is True
                          and r["body"]["response_format"]["json_schema"]["name"] == "cards" for r in _rs))
fails += report_check("the strict ADAPTER ran (additionalProperties=false, every property required) — same as realtime",
                      all(r["body"]["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
                          and set(r["body"]["response_format"]["json_schema"]["schema"]["required"]) == {"results"}
                          for r in _rs))
_no, _ = submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", system="t")
_rn = _read_built(_no)
os.unlink(_no)
fails += report_check("no schema → no response_format (a caller that wants free text gets free text)",
                      all("response_format" not in r["body"] for r in _rn))

print("\n-- a schema strict mode cannot serve is REFUSED at build, before any upload; no partial temp survives --")
OVER = {"type": "object", "properties": {f"f{i}": {"type": "string", "enum": [f"v{j}" for j in range(300)]}
                                          for i in range(4)}, "required": [f"f{i}" for i in range(4)]}   # 1200 > 1000
_before = set(os.listdir(tempfile.gettempdir()))
_refused = None
try:
    submit.build_chat_batch_jsonl(tasks_p, "gpt-5.6-luna", schema=OVER)
except adapters.SchemaNotStrictExpressible as e:
    _refused = str(e)
fails += report_check("an over-limit enum schema raises SchemaNotStrictExpressible (typed, not a 400 per row later)",
                      _refused is not None and "1200" in _refused and str(adapters.OPENAI_STRICT_MAX_ENUM_VALUES) in _refused)
_leaked = [p for p in set(os.listdir(tempfile.gettempdir())) - _before if p.startswith("spendguard-batch-req-")]
fails += report_check("the refusal leaves NO partial envelope behind", not _leaked)

print("\n-- per-task overrides: a line's own system / schema win; lines without them use the shared defaults --")
MIX = [{"custom_id": "shared", "content": "x"},
       {"custom_id": "own", "content": "y", "system": "OWN SYSTEM", "schema": SCHEMA, "schema_name": "own_shape"}]
mix_p = _write_tasks(MIX)
_bm, nm = submit.build_chat_batch_jsonl(mix_p, "gpt-5.6-luna", system="SHARED")
_rm = {r["custom_id"]: r["body"] for r in _read_built(_bm)}
os.unlink(_bm)
os.unlink(mix_p)
fails += report_check("both lines built, custom_ids verbatim", nm == 2 and set(_rm) == {"shared", "own"})
fails += report_check("the shared line uses the shared system and carries no response_format",
                      _rm["shared"]["messages"][0]["content"] == "SHARED" and "response_format" not in _rm["shared"])
fails += report_check("the overriding line uses ITS system and ITS schema under ITS name",
                      _rm["own"]["messages"][0]["content"] == "OWN SYSTEM"
                      and _rm["own"]["response_format"]["json_schema"]["name"] == "own_shape")

os.unlink(tasks_p)
print(f"\n{'[FAIL]' if fails else 'OK'} test_batch_envelope_build: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
