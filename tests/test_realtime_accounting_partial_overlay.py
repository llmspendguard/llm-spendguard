"""Realtime accounting survives a long-lived process whose calls module was partially overlaid.

The delegate failure came from a newer ``record_call`` body running in an older module namespace:
the body referenced the module-global ``_truthy``, but that global was absent.  ``_rt_account`` then
swallowed the NameError and emitted ``real-time accounting failed``, leaving the attributed call row
unwritten.  Recreate that exact resident-module state and drive provider-reported realtime usage
through the complete ``_rt_account -> _record_rt -> calls.record_call`` path.

Offline and hermetic: the money writer is captured in memory and the calls ledger is SQLite memory.
"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rt-overlay-")
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import calls, config, gate


MODEL = "gpt-5.6-luna"
PROVIDER_INPUT_TOKENS = 120
PROVIDER_OUTPUT_TOKENS = 30
TEST_INTENT = "test-realtime-overlay"



money_rows = []
recording_errors = []
call_writer_returns = []
calls_ledger = sqlite3.connect(":memory:", check_same_thread=False)
calls._ensure_call_observation_schema(calls_ledger)
original_rt_record = gate._rt_record
original_truthy = calls.__dict__.pop("_truthy")
original_calls_db = calls._calls_db
original_record_call = calls.record_call
original_rollback = config.rollback_ledger_conn
previous_context = dict(calls.current())
previous_calls_enabled = os.environ.get("SPENDGUARD_CALLS")
os.environ["SPENDGUARD_CALLS"] = "1"
gate._rt_record = lambda provider, model, cost, *args, **kw: money_rows.append(
    {"provider": provider, "model": model, "cost": cost, "args": args, **kw}
)
calls._calls_db = lambda: calls_ledger
def capture_record_call(*args, **kwargs):
    call_writer_returns.append(original_record_call(*args, **kwargs))
    return call_writer_returns[-1]
calls.record_call = capture_record_call
config.rollback_ledger_conn = lambda name: recording_errors.append(sys.exc_info()[1])
calls.set_context(intent=TEST_INTENT)

result = types.SimpleNamespace(
    usage=types.SimpleNamespace(
        prompt_tokens=PROVIDER_INPUT_TOKENS,
        completion_tokens=PROVIDER_OUTPUT_TOKENS,
    ),
    choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content="accounted"),
        finish_reason="stop",
    )],
)
request = {"model": MODEL, "messages": [{"role": "user", "content": "record this call"}]}
stderr = io.StringIO()

try:
    with contextlib.redirect_stderr(stderr):
        gate._rt_account(MODEL, request, result, gate._est_oai_chat, gate._act_oai_chat)
    call_rows = calls_ledger.execute(
        "SELECT intent, in_tok, out_tok, executor FROM calls WHERE kind='realtime'"
    ).fetchall()
finally:
    gate._rt_record = original_rt_record
    calls.__dict__["_truthy"] = original_truthy
    calls._calls_db = original_calls_db
    calls.record_call = original_record_call
    config.rollback_ledger_conn = original_rollback
    calls._local.ctx = previous_context
    if previous_calls_enabled is None:
        os.environ.pop("SPENDGUARD_CALLS", None)
    else:
        os.environ["SPENDGUARD_CALLS"] = previous_calls_enabled
    calls_ledger.close()

failures = []
warning_text = stderr.getvalue()
record_script_check(failures, "provider-reported money reached the realtime recorder exactly once", len(money_rows) == 1)
record_script_check(failures, "gate and test share the same calls module", gate._calls is calls)
record_script_check(failures, "the isolated process did not disable the calls ledger", not config.is_explicit_true(os.getenv("SPENDGUARD_NO_LEDGER")))
record_script_check(failures, "the attributed call writer completed without a swallowed exception", not recording_errors)
record_script_check(failures, "the attributed call writer returned a call id", bool(call_writer_returns and call_writer_returns[0]))
record_script_check(failures, "the attributed realtime call row was written exactly once", len(call_rows) == 1)
record_script_check(failures, "actual tokens and metered executor attribution survived the resident-module state", call_rows == [(TEST_INTENT, PROVIDER_INPUT_TOKENS, PROVIDER_OUTPUT_TOKENS, "api")])
record_script_check(failures, "no real-time accounting failed warning was emitted", "real-time accounting failed" not in warning_text)
record_script_check(failures, "no NameError escaped into the accounting warning", "NameError" not in warning_text)

print(("[OK]" if not failures else "[FAIL]")
      + f" realtime partial-overlay accounting: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
