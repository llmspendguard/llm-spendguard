"""Offline guards: refused batches book nothing; collect revises the accepted estimate to measured actuals."""
import os
import sqlite3
import sys
import tempfile
import types

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-batch-settle-")
os.environ["SPENDGUARD_CALLS"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "sk-test-fake-not-real"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import callio, calls, config, gate, pricing  # noqa: E402
config.budget_backend = lambda: "sqlite"

failures = []


def check_batch_booking(name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + name)
    if not condition:
        failures.append(name)


intent = "test:batch-settle"
model = "claude-haiku-4-5"
estimate = {"provider": "anthropic", "model": model, "requests": 2, "in_tok": 1000, "out_tok": 400,
            "cost": pricing.batch_cost(model, 1000, 400, provider="anthropic")}

print("-- refusal happens before booking --")
calls.set_context(intent=intent, defer_batch_booking=True)
try:
    gate._decide_and_account({**estimate, "cost": gate._cap() + 1})
except gate.SpendGateRefused:
    pass
con = sqlite3.connect(config.db_path())
has_spend = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone()
spend_count = con.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0] if has_spend else 0
has_calls = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
call_count = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0] if has_calls else 0
check_batch_booking("a REFUSED submit books zero spend rows", spend_count == 0)
check_batch_booking("a REFUSED submit books zero calls rows", call_count == 0)

print("\n-- accepted estimate is revised in place when measured usage is collected --")
gate.record_accepted_batch_estimate(estimate)


class FakeMessageBatchClient:
    class messages:
        class batches:
            @staticmethod
            def retrieve(_batch_id):
                return types.SimpleNamespace(processing_status="ended", results_url="memory://results")

            @staticmethod
            def results(_batch_id):
                usage = types.SimpleNamespace(input_tokens=100, output_tokens=25,
                                              cache_read_input_tokens=10, cache_creation_input_tokens=0)
                message = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="ok")], usage=usage)
                result = types.SimpleNamespace(type="succeeded", message=message)
                return [types.SimpleNamespace(custom_id="one", result=result)]


original_client = callio._anthropic_client
callio._anthropic_client = lambda: FakeMessageBatchClient()
try:
    result = callio.collect_message_batch("batch-test", intent, model)
finally:
    callio._anthropic_client = original_client

actual_cost = pricing.batch_cost(model, 110, 25, 10, provider="anthropic")
money = con.execute("SELECT batch_usd,in_tok,out_tok,cost_basis FROM spend_events ORDER BY rowid DESC LIMIT 1").fetchone()
corpus = con.execute("SELECT cost,in_tok,out_tok FROM calls WHERE kind='batch' ORDER BY rowid DESC LIMIT 1").fetchone()
check_batch_booking("collect reports the measured usage", result["usage"]["in_tok"] == 110 and result["usage"]["out_tok"] == 25)
check_batch_booking("collect reconciles the money row estimate to measured actual tokens and billed dollars",
                    money and abs(float(money[0]) - actual_cost) < 1e-12 and money[1:] == (110, 25, "billed"))
check_batch_booking("collect reconciles the calls row in place to measured actuals",
                    corpus and abs(corpus[0] - actual_cost) < 1e-12 and corpus[1:] == (110, 25))
con.close()

print(f"\n{'[FAIL]' if failures else 'OK'} test_batch_booking_collect_reconcile: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
