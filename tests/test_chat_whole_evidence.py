"""Non-vacuous script guard for whole chat evidence and complete structured replies."""
import contextlib
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-chat-whole-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402
from spendguard import config, pricing  # noqa: E402
from spendguard.generate.spend_capture import chat  # noqa: E402
from spendguard.generate.execution import execution_accessors  # noqa: E402
from spendguard.store.call_corpus import call_corpus_accessors  # noqa: E402

fails = []
records = [f"title-{index}-" + chr(65 + index) * 401 for index in range(3)]
old_cfg_get = config._cfg_get
old_input = pricing.max_input_tokens
old_output = pricing.max_output_tokens
old_toklen = chat._toklen
config._cfg_get = lambda section, key, default=None: 700 if key == "synth_input_tokens" else (57 if key == "synth_output_tokens" else default)
pricing.max_input_tokens = lambda model: 700
pricing.max_output_tokens = lambda model: 300
chat._toklen = len
plan = chat._chat_whole_evidence_plan(records, "prefix\n", "system", "model")
joined = "\n".join(plan[0])
record_script_check(fails, "whole-evidence: every complete record reaches a planned prompt",
                    all(record in joined for record in records) and len(plan[0]) > 1)
oversized_named = False
try:
    chat._chat_whole_evidence_plan(["Z" * 701], "prefix\n", "system", "model")
except ValueError as exc:
    oversized_named = "chat_oversized_record_gap" in str(exc)
record_script_check(fails, "whole-evidence: oversized single record fails closed with a named gap", oversized_named)

old_call = execution_accessors.resolve_call
old_context = call_corpus_accessors.resolve_calls_context
call_corpus_accessors.resolve_calls_context = lambda **kwargs: contextlib.nullcontext()
for metadata in ({"truncated": True}, {"finish_reason": "length"}, {"out_tok": 57}):
    execution_accessors.resolve_call = lambda *args, _metadata=metadata, **kwargs: {"text": "{}", **_metadata}
    result = chat._chat_execute_evidence_plan("model", (["prefix\nrecord"], 57, 700, 0),
                                              "system", "prefix\n", "intent")
    record_script_check(fails, f"reply-guard: {metadata} is a named incomplete gap",
                        result.get("error", "").startswith("chat_reply_incomplete_gap"))

allocation = chat._chat_allocation_prompt({}, [{"title": "T" * 401, "summary": "S" * 401,
                                                 "first_user": "U" * 401}])
record_script_check(fails, "allocation: title, summary, and first_user are all passed whole",
                    "T" * 401 in allocation and "S" * 401 in allocation and "U" * 401 in allocation)
execution_accessors.resolve_call = old_call
call_corpus_accessors.resolve_calls_context = old_context
config._cfg_get = old_cfg_get
pricing.max_input_tokens = old_input
pricing.max_output_tokens = old_output
chat._toklen = old_toklen
print(("\n[FAIL] " if fails else "\n[OK] ") + f"chat_whole_evidence: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
