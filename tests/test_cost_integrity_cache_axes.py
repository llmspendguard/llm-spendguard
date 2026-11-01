"""Offline guard: cache axes, legacy gaps, and injected pricing/schema failures."""
import math
import datetime
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-cache-axes-"))
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"

from spendguard import calls, cost_integrity, ledger_read, pricing

CACHE_AXIS_MODEL = "claude-opus-4-8"
CACHE_AXIS_FRESH = 1234
CACHE_AXIS_OUTPUT = 321
CACHE_AXIS_READ = 80000
CACHE_AXIS_WRITE = 7000
CACHE_AXIS_DAYS = 7
CACHE_AXIS_CALLS = 1
CACHE_AXIS_DAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
CACHE_AXIS_KINDS = ("realtime", "batch")
CACHE_AXIS_FAILURE = "injected pricing outage"
CACHE_AXIS_DATABASE_NAME = "cache_axes.sqlite"

for cache_axis_kind in CACHE_AXIS_KINDS:
    cache_axis_rates = pricing.price(CACHE_AXIS_MODEL)
    cache_axis_input_key, cache_axis_output_key = (("batch_in", "batch_out")
                                                 if cache_axis_kind == "batch" else ("in_", "out"))
    cache_axis_read_key = "batch_cached_in" if cache_axis_kind == "batch" else "cached_in"
    cache_axis_expected = (CACHE_AXIS_FRESH * cache_axis_rates[cache_axis_input_key]
                           + CACHE_AXIS_OUTPUT * cache_axis_rates[cache_axis_output_key]
                           + CACHE_AXIS_READ * cache_axis_rates[cache_axis_read_key]
                           + CACHE_AXIS_WRITE * cache_axis_rates[cache_axis_input_key]
                           * pricing.CACHE_WRITE_5M_MULTIPLIER) / pricing.ONE_MILLION
    cache_axis_collapsed = (CACHE_AXIS_FRESH + CACHE_AXIS_READ + CACHE_AXIS_WRITE) * (
        cache_axis_rates[cache_axis_input_key] / pricing.ONE_MILLION)
    cache_axis_row = (CACHE_AXIS_MODEL, CACHE_AXIS_DAY, cache_axis_kind, CACHE_AXIS_CALLS,
                      cache_axis_expected, CACHE_AXIS_FRESH, CACHE_AXIS_OUTPUT, CACHE_AXIS_READ, CACHE_AXIS_WRITE)
    cache_axis_gaps = []
    assert cost_integrity.classify_impossible_cost_buckets([cache_axis_row], gaps=cache_axis_gaps) == ([], 0)
    assert not cache_axis_gaps
    assert math.isclose(cost_integrity._reconstruct_split_token_cost(
        CACHE_AXIS_MODEL, CACHE_AXIS_FRESH, CACHE_AXIS_OUTPUT, CACHE_AXIS_READ, CACHE_AXIS_WRITE,
        cache_axis_kind == "batch"), cache_axis_expected)
    # Over- and undercharges must both be found, including amounts below the former ceiling.
    for cache_axis_wrong in (cache_axis_collapsed, cache_axis_expected / pricing.CACHE_WRITE_1H_MULTIPLIER):
        cache_axis_bad = (*cache_axis_row[:4], cache_axis_wrong, *cache_axis_row[5:])
        cache_axis_findings, cache_axis_unpriced = cost_integrity.classify_impossible_cost_buckets([cache_axis_bad])
        assert not cache_axis_unpriced and len(cache_axis_findings) == CACHE_AXIS_CALLS
        assert math.isclose(cache_axis_findings[0]["expected_cost"], cache_axis_expected)

cache_axis_realtime_cost = pricing.realtime_cost(CACHE_AXIS_MODEL, CACHE_AXIS_FRESH + CACHE_AXIS_READ,
                                                CACHE_AXIS_OUTPUT, CACHE_AXIS_READ,
                                                cache_creation_tok=CACHE_AXIS_WRITE)
cache_axis_path = Path(os.environ["SPENDGUARD_HOME"]) / CACHE_AXIS_DATABASE_NAME
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    calls._ensure_call_observation_schema(cache_axis_connection)
    with patch.object(calls, "_calls_db", return_value=cache_axis_connection):
        calls.record_call.__wrapped__(
            __name__, CACHE_AXIS_MODEL, CACHE_AXIS_KINDS[0], cache_axis_realtime_cost,
            in_tok=CACHE_AXIS_FRESH + CACHE_AXIS_READ, out_tok=CACHE_AXIS_OUTPUT,
            cache_read_tok=CACHE_AXIS_READ, cache_write_tok=CACHE_AXIS_WRITE,
            intent=__name__, project=__name__)
cache_axis_rows = ledger_read.read_cost_integrity_buckets(CACHE_AXIS_DAYS, cache_axis_path)
assert cost_integrity.classify_impossible_cost_buckets(row.values for row in cache_axis_rows) == ([], 0)
assert cache_axis_rows[0]["i"] == CACHE_AXIS_FRESH
assert cache_axis_rows[0]["cache_read_tok"] == CACHE_AXIS_READ
assert cache_axis_rows[0]["cache_write_tok"] == CACHE_AXIS_WRITE
# A missing derived nano mirror must not understate authoritative recorded cost.
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    cache_axis_connection.execute("UPDATE calls SET cost_nano=NULL")
cache_axis_nano_gap_rows = ledger_read.read_cost_integrity_buckets(CACHE_AXIS_DAYS, cache_axis_path)
assert cost_integrity.classify_impossible_cost_buckets(
    row.values for row in cache_axis_nano_gap_rows) == ([], 0)
# Prove the same check still fails for a genuinely wrong authoritative cost.
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    cache_axis_connection.execute("UPDATE calls SET cost=cost*?", (pricing.CACHE_WRITE_1H_MULTIPLIER,))
cache_axis_wrong_cost_rows = ledger_read.read_cost_integrity_buckets(CACHE_AXIS_DAYS, cache_axis_path)
cache_axis_wrong_findings, cache_axis_wrong_unpriced = cost_integrity.classify_impossible_cost_buckets(
    row.values for row in cache_axis_wrong_cost_rows)
assert not cache_axis_wrong_unpriced and len(cache_axis_wrong_findings) == CACHE_AXIS_CALLS
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    cache_axis_connection.execute("UPDATE calls SET cost=?", (cache_axis_realtime_cost,))
# Fault dimension: partially populated bucket cannot silently SUM past a missing cache component.
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    with patch.object(calls, "_calls_db", return_value=cache_axis_connection):
        calls.record_call.__wrapped__(
            __name__, CACHE_AXIS_MODEL, CACHE_AXIS_KINDS[0], cache_axis_realtime_cost,
            in_tok=CACHE_AXIS_FRESH, out_tok=CACHE_AXIS_OUTPUT,
            cache_read_tok=None, cache_write_tok=CACHE_AXIS_WRITE,
            intent=__name__, project=__name__)
cache_axis_rows = ledger_read.read_cost_integrity_buckets(CACHE_AXIS_DAYS, cache_axis_path)
assert len(cache_axis_rows) == len(CACHE_AXIS_KINDS)
assert any(row["cache_read_tok"] is None for row in cache_axis_rows)
assert any(row["cache_read_tok"] == CACHE_AXIS_READ for row in cache_axis_rows)
cache_axis_gaps = []
cost_integrity.classify_impossible_cost_buckets((row.values for row in cache_axis_rows), gaps=cache_axis_gaps)
assert any(gap["gap"] == "legacy_cache_split_missing" for gap in cache_axis_gaps)
with patch.object(pricing, "price", side_effect=RuntimeError(CACHE_AXIS_FAILURE)):
    cache_axis_gaps = []
    assert cost_integrity.classify_impossible_cost_buckets([cache_axis_row], gaps=cache_axis_gaps) == ([], 1)
    assert CACHE_AXIS_FAILURE in cache_axis_gaps[0]["error"] and "Traceback" in cache_axis_gaps[0]["error"]
cache_axis_missing_rates = {key: value for key, value in cache_axis_rates.items() if key != cache_axis_read_key}
with patch.object(pricing, "price", return_value=cache_axis_missing_rates):
    cache_axis_gaps = []
    assert cost_integrity.classify_impossible_cost_buckets([cache_axis_row], gaps=cache_axis_gaps) == ([], 1)
    assert "token_cost_reconstruction_unavailable" == cache_axis_gaps[0]["gap"]
with patch.object(ledger_read, "read_cost_integrity_buckets", side_effect=RuntimeError(CACHE_AXIS_FAILURE)):
    cache_axis_audit = cost_integrity.audit_metered_cost_vs_tokens()
    assert not cache_axis_audit["ok"] and "Traceback" in cache_axis_audit["error"]
# Old schema is usable only as named best effort, never as verified zero-cache usage.
with sqlite3.connect(cache_axis_path) as cache_axis_connection:
    for cache_axis_column in ledger_read.COST_INTEGRITY_CACHE_COLUMNS:
        cache_axis_connection.execute(f"ALTER TABLE calls DROP COLUMN {cache_axis_column}")
cache_axis_rows = ledger_read.read_cost_integrity_buckets(CACHE_AXIS_DAYS, cache_axis_path)
assert all(cache_axis_rows[0][column] is None for column in ledger_read.COST_INTEGRITY_CACHE_COLUMNS)
with patch.object(ledger_read, "read_cost_integrity_buckets", return_value=cache_axis_rows):
    assert any("legacy_cache_split_missing" in line for line in cost_integrity.cost_integrity_lines())
print("OK cache axis reconstruction, legacy coverage, and injected failures")

# Provider observations must reach the real calls writer, not just hand-built SQL fixtures.
from spendguard import gate, guard, bulkgate

cache_axis_usage = SimpleNamespace(input_tokens=CACHE_AXIS_FRESH, output_tokens=CACHE_AXIS_OUTPUT,
    cache_read_input_tokens=CACHE_AXIS_READ, cache_creation_input_tokens=CACHE_AXIS_WRITE,
    cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=0, ephemeral_5m_input_tokens=CACHE_AXIS_WRITE))
cache_axis_result = SimpleNamespace(usage=cache_axis_usage, content=[], stop_reason=None)
with patch.dict(os.environ, {"SPENDGUARD_CALLS": "1"}), patch.object(gate, "_rt_record"), \
        patch.object(gate, "_record_tool_fees"), patch.object(gate, "_meta_intent", return_value=False), \
        patch.object(guard, "record_saving"), patch.object(bulkgate, "note_response"):
    gate._rt_account(CACHE_AXIS_MODEL, {}, cache_axis_result, gate._est_anth_msg, gate._act_anth_msg)
    with calls._calls_db() as cache_axis_production_db:
        cache_axis_observed = cache_axis_production_db.execute(
            "SELECT in_tok,cache_read_tok,cache_write_tok FROM calls ORDER BY ts DESC LIMIT ?",
            (CACHE_AXIS_CALLS,)).fetchone()
    assert tuple(cache_axis_observed) == (CACHE_AXIS_FRESH + CACHE_AXIS_READ, CACHE_AXIS_READ, CACHE_AXIS_WRITE)
    cache_axis_usage.cache_creation = SimpleNamespace(ephemeral_1h_input_tokens=CACHE_AXIS_WRITE,
                                                      ephemeral_5m_input_tokens=0)
    with patch.object(calls, "record_call", wraps=calls.record_call) as cache_axis_record_spy:
        gate._rt_account(CACHE_AXIS_MODEL, {}, cache_axis_result, gate._est_anth_msg, gate._act_anth_msg)
    assert cache_axis_record_spy.call_args.kwargs["cache_write_tok"] is None
    assert cache_axis_record_spy.call_args.kwargs["cache_read_tok"] == CACHE_AXIS_READ
print("OK provider cache observations reach calls; one-hour writes remain an explicit gap")
