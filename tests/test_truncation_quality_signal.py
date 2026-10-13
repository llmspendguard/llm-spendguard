"""Provider-declared truncation is authoritative BAD quality for new call records. Offline, zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-trunc-quality-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import callio, calls  # noqa: E402

failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


intent = "test:truncation-quality"
model = "gpt-test"

complete_id = calls.record_call("openai", model, "realtime", 0.01, intent=intent, finish="stop")
complete = calls._calls_db().execute(
    "SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (complete_id,)
).fetchone()
check("a non-truncated call remains unlabeled", complete == (None, None, None))
calls.feedback(complete_id, ok=True)
before = calls.cost_summary(intent)[0]
check("the ordinary good label produces 100% good evidence", before[4:6] == (1, 0))

truncated_id = calls.record_call("openai", model, "realtime", 0.01, intent=intent, finish="length")
truncated = calls._calls_db().execute(
    "SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (truncated_id,)
).fetchone()
check("a truncated call is recorded bad with authoritative provenance", truncated == ("bad", "truncation", 1.0))
calls.feedback(truncated_id, ok=True)
protected = calls._calls_db().execute(
    "SELECT quality, quality_src, quality_conf FROM calls WHERE id=?", (truncated_id,)
).fetchone()
check("later good feedback cannot overwrite truncation", protected == truncated)
after = calls.cost_summary(intent)[0]
check("cost_summary quality falls from 100% to 50%", after[4:6] == (1, 1))

io_complete = callio.record_io_sample(intent, "openai", model, "b", "complete", "p", "o",
                                      finish_reason="stop")
io_truncated = callio.record_io_sample(intent, "openai", model, "b", "truncated", "p", "o",
                                       finish_reason="max_tokens")
io_rows = callio._callio_db().execute(
    "SELECT id, quality, quality_src, quality_conf FROM call_io ORDER BY custom_id"
).fetchall()
check("call_io keeps the identical complete reply unlabeled", io_rows[0][0] == io_complete and io_rows[0][1:] == (None, None, None))
check("call_io records the truncated reply bad", io_rows[1][0] == io_truncated and io_rows[1][1:] == ("bad", "truncation", 1.0))
callio.set_quality(io_truncated, True, src="judge", conf=0.95)
rate = callio.good_rates()[(intent, model)]
check("call_io truncation remains bad and enters its quality aggregate", rate["judged"] == 1 and rate["good_rate"] == 0.0)

print(f"\n{'[FAIL]' if failures else 'OK'} test_truncation_quality_signal: {len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
