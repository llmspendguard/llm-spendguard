"""Offline guards for the corrected Flash rate class, one-row bakeoff attribution, and ISO timestamp window."""
import datetime
import os
import sqlite3
import sys
import tempfile

test_home = tempfile.mkdtemp(prefix="sg-cost-attribution-")
os.environ["SPENDGUARD_HOME"] = test_home
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls, config, cost_integrity, model_catalog, pricing

failures = []


def assert_cost_attribution(outcomes, label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        outcomes.append(label)


attribution_catalog_rates = model_catalog.as_price_table()
flash_entries = [(model, rates) for model, rates in attribution_catalog_rates["gemini"].items()
                 if "#gemini-3-flash-preview" in rates.get("_source", "")]
assert_cost_attribution(failures, "official-source Flash price entry is discoverable from the SSOT", len(flash_entries) == 1)
flash_model, flash_rates = flash_entries[0]
assert_cost_attribution(failures, "realtime rates are twice batch rates on both token axes",
                        flash_rates["in_"] == 2 * flash_rates["batch_in"] and
                        flash_rates["out"] == 2 * flash_rates["batch_out"])
sample_in, sample_out = 1096, 66
recorded = pricing.realtime_cost(flash_model, sample_in, sample_out)
findings, _ = cost_integrity.classify_impossible_cost_buckets(
    [(flash_model, "sample-day", "realtime", 1, recorded, sample_in, sample_out)])
assert_cost_attribution(failures, "the formerly flagged 2x class is consistent at the corrected realtime rate", not findings)

call_id = "offline-bakeoff-call"
calls.record_call("gemini", flash_model, "realtime", recorded, in_tok=sample_in, out_tok=sample_out,
                  intent="offline-bakeoff", who="spendguard:test", executor="api", call_id=call_id,
                  requested_model="requested-alias", served_model=flash_model, resolved_lane="api")
calls.finalize_bakeoff_measurement(call_id, "gemini", "requested-alias", flash_model, recorded,
                                   sample_in, sample_out, "offline-bakeoff", "good", None, "api")
connection = sqlite3.connect(config.db_path())
rows = connection.execute("SELECT caller,requested_model,served_model,resolved_lane FROM calls WHERE id=?", (call_id,)).fetchall()
assert_cost_attribution(failures, "one physical bakeoff leg produces exactly one spend row", len(rows) == 1)
assert_cost_attribution(failures, "the one row carries requested, served, and lane attribution",
                        rows == [("bakeoff", "requested-alias", flash_model, "api")])

now = datetime.datetime.now(datetime.timezone.utc)
old_ts = (now - datetime.timedelta(days=30)).isoformat(timespec="seconds")
new_ts = (now - datetime.timedelta(days=1)).isoformat(timespec="seconds")
for row_id, stamp in (("window-old", old_ts), ("window-new", new_ts)):
    connection.execute("INSERT INTO calls (id,ts,model,kind,cost,in_tok,out_tok) VALUES (?,?,?,?,?,?,?)",
                       (row_id, stamp, flash_model, "realtime", recorded, sample_in, sample_out))
connection.commit()
connection.close()
window = cost_integrity.audit_metered_cost_vs_tokens(since_days=7)
assert_cost_attribution(failures, "TEXT ISO-8601 timestamps honor since_days on both sides of the boundary",
                        window.get("ok") is True and window.get("checked") == 2)

print(f"\n{'OK' if not failures else 'FAIL'} test_cost_attribution_regressions_20261016: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
