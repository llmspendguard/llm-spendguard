"""Durable best-value verdict cache: cross-interpreter recall, invalidation, and never-poison proofs.

Offline: every advisor function is replaced before selection; no network or paid LLM call is possible.
"""
import os
import pathlib
import subprocess
import sys
import tempfile


_TEST_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEST_HOME = tempfile.mkdtemp(prefix="spendguard-bestvalue-l2-")
_COUNTER_PATH = pathlib.Path(_TEST_HOME) / "advisor-count"
_TEST_INTENT = "test:durable-best-value"
_TEST_MODEL = "openai:test-fixed-pick"
_FP_ONE = "evidence-fingerprint-one"
_FP_TWO = "evidence-fingerprint-two"
_FAKE_API_KEY = "sk-test-bestvalue-cache-no-network"

_ENV = os.environ.copy()
_ENV.update({
    "SPENDGUARD_HOME": _TEST_HOME,
    "SPENDGUARD_TEST_ISOLATED": "1",
    "OPENAI_API_KEY": _FAKE_API_KEY,
    "PYTHONPATH": str(_TEST_ROOT / "src"),
})

_COMMON = """
import json
import pathlib
import sqlite3
from spendguard import advisor, best_value, config, gate, models
intent = %r
picked_model = %r
counter_path = pathlib.Path(%r)
models.effort_for = lambda model, intent: "low"
""" % (_TEST_INTENT, _TEST_MODEL, str(_COUNTER_PATH))


def _run_cache_proof_phase(label, body, expect_ok=True):
    result = subprocess.run(
        [sys.executable, "-c", _COMMON + body],
        cwd=str(_TEST_ROOT),
        env=_ENV,
        text=True,
        capture_output=True,
    )
    ok = result.returncode == 0
    print("  [%s] %s" % ("OK" if ok == expect_ok else "FAIL", label))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if ok != expect_ok:
        raise AssertionError("%s: expected success=%s, returncode=%s" % (label, expect_ok, result.returncode))
    return result


print("-- subprocess A: derive once and persist --")
_run_cache_proof_phase("A called advisor once and wrote bestvalue_verdict", """
best_value._evidence_fingerprint = lambda intent: %r
def fixed_advisor_pick(*args, **kwargs):
    count = int(counter_path.read_text()) if counter_path.exists() else 0
    counter_path.write_text(str(count + 1))
    return {"top": [{"id": picked_model, "why": "fixed test pick"}], "ranked_from": 1, "note": "offline"}
advisor.recommend_models = fixed_advisor_pick
result = best_value.select_model_effort(intent, "openai:requested", quality_target="high")
assert result["model"] == picked_model
assert counter_path.read_text() == "1"
row = best_value._bestvalue_verdict_db().execute(
    "SELECT evidence_fp FROM bestvalue_verdict WHERE intent=?", (intent,)).fetchone()
assert row == (%r,), row
print(json.dumps({"model": result["model"], "advisor_calls": 1, "stored_fp": row[0]}))
""" % (_FP_ONE, _FP_ONE))

print("-- subprocess B: fresh interpreter recalls L2 and must not call advisor --")
_run_cache_proof_phase("B returned the same model from L2 with zero new advisor calls", """
best_value._evidence_fingerprint = lambda intent: %r
def advisor_must_not_run(*args, **kwargs):
    raise AssertionError("recommend_models called on durable-cache hit")
advisor.recommend_models = advisor_must_not_run
result = best_value.select_model_effort(intent, "openai:requested", quality_target="high")
assert result["model"] == picked_model
assert result["considered"].get("l2") is True
assert result["considered"].get("cached") is True
assert counter_path.read_text() == "1"
print(json.dumps({"model": result["model"], "l2": True, "advisor_calls": 1}))
""" % _FP_ONE)

print("-- fingerprint mutation: miss old row, re-derive, and prune stale fp --")
_run_cache_proof_phase("changed fingerprint re-derived and retained only the live row", """
best_value._evidence_fingerprint = lambda intent: %r
def changed_evidence_pick(*args, **kwargs):
    count = int(counter_path.read_text())
    counter_path.write_text(str(count + 1))
    return {"top": [{"id": picked_model, "why": "changed evidence"}], "ranked_from": 1, "note": "offline"}
advisor.recommend_models = changed_evidence_pick
result = best_value.select_model_effort(intent, "openai:requested", quality_target="high")
assert result["model"] == picked_model
assert result["considered"].get("cached") is False
assert counter_path.read_text() == "2"
rows = best_value._bestvalue_verdict_db().execute(
    "SELECT evidence_fp FROM bestvalue_verdict WHERE intent=? AND quality_target=?",
    (intent, "high"),
).fetchall()
assert rows == [(%r,)], rows
print(json.dumps({"advisor_calls": 2, "retained_fps": [row[0] for row in rows]}))
""" % (_FP_TWO, _FP_TWO))

print("-- never-poison: transient failure and deliberate stop write nothing --")
_run_cache_proof_phase("transient advisor failure wrote no L2 row", """
failure_intent = intent + ":transient"
best_value._evidence_fingerprint = lambda intent: "transient-fp"
def transient_advisor_failure(*args, **kwargs):
    raise RuntimeError("offline transient failure")
advisor.recommend_models = transient_advisor_failure
result = best_value.select_model_effort(failure_intent, "openai:requested")
assert result["model"] is None
count = best_value._bestvalue_verdict_db().execute(
    "SELECT COUNT(*) FROM bestvalue_verdict WHERE intent=?", (failure_intent,)).fetchone()[0]
assert count == 0, count
print(json.dumps({"transient_rows": count}))
""")

_run_cache_proof_phase("deliberate stop propagated and wrote no L2 row", """
stop_intent = intent + ":deliberate-stop"
best_value._evidence_fingerprint = lambda intent: "deliberate-stop-fp"
def deliberate_advisor_stop(*args, **kwargs):
    raise gate.SpendGateRefused("planted offline refusal")
advisor.recommend_models = deliberate_advisor_stop
try:
    best_value.select_model_effort(stop_intent, "openai:requested")
except gate.deliberate_stop_types():
    pass
else:
    raise AssertionError("deliberate stop was swallowed")
count = best_value._bestvalue_verdict_db().execute(
    "SELECT COUNT(*) FROM bestvalue_verdict WHERE intent=?", (stop_intent,)).fetchone()[0]
assert count == 0, count
print(json.dumps({"deliberate_stop_propagated": True, "deliberate_stop_rows": count}))
""")

print("\nOK test_bestvalue_verdict_cache_persists: cross-process recall + mutation + never-poison")
