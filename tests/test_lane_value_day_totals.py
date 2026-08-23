"""lane_value.day_totals — the SERVER-PUSH twin of stamp_from_ledger: the ledger-valued lanes' (gemini/zai) plan
VALUE as /v1/ledger est-value rows (billed=False, channel=<lane>), the client half of the server's
EST_VALUE_CHANNELS. Guards: the zai-coding→'zai' channel quirk + gemini→'gemini' identity, billed=False +
kind=workload, per-(project,day,model,lane) aggregation with call counts, an unpriced ($0) row STILL counted as
work (not dropped), and a day-less row excluded (never silently — it's counted/warned). Offline: _subscription_rows
+ pricing stubbed, no DB, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-laneval-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_value, lane_catalog                                        # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- _channel_for: the zai-coding→'zai' quirk, identity otherwise --")
fails += ck("zai-coding→zai, gemini→gemini, codex→codex (identity)",
            lane_value._channel_for("zai-coding") == "zai"
            and lane_value._channel_for("gemini") == "gemini"
            and lane_value._channel_for("codex") == "codex")

# stub the valued lanes + their subscription rows + pricing (no DB)
lane_value.ledger_valued_lanes = lambda: {"gemini", "zai-coding"}
ROWS = [
    ("gemini", "2026-08-10T01:00:00Z", "google", "gemini-3.7-flash-low", 1000, 200, "lmm"),
    ("gemini", "2026-08-10T02:00:00Z", "google", "gemini-3.7-flash-low", 500, 100, "lmm"),    # same key → aggregates
    ("zai-coding", "2026-08-11T00:00:00Z", "zai", "glm-5.3", 800, 300, "manga2anime"),
    ("gemini", "2026-08-12T00:00:00Z", "google", "unpriced-model", 400, 100, "lmm"),           # $0 → still WORK
    ("gemini", "", "google", "gemini-3.7-flash-low", 100, 50, "lmm"),                           # no day → excluded
]
lane_value._subscription_rows = lambda since, valued: ROWS
lane_catalog.use_name_cost = lambda model, i, o, lane=None: 0.0 if model == "unpriced-model" else (i + o) * 3e-6

print("\n-- day_totals: channels, billed=False, aggregation, unpriced-still-counted, day-less-excluded --")
rows = lane_value.day_totals("ash@healiom.com")
chans = {r["channel"] for r in rows}
fails += ck("zai-coding lane pushes under the 'zai' channel (not 'zai-coding')", "zai" in chans and "zai-coding" not in chans)
fails += ck("gemini lane pushes under 'gemini'", "gemini" in chans)
fails += ck("every row is billed=False + kind=workload (the est-value axis)",
            rows and all(r["billed"] is False and r["kind"] == "workload" for r in rows))
by = {(r["project"], r["day"], r["channel"]): r for r in rows}
g = by.get(("lmm", "2026-08-10", "gemini"))
fails += ck("same (project,day,model,lane) aggregated → 2 calls, summed value", g and g["calls"] == 2 and g["spend_micros"] > 0)
fails += ck("unpriced ($0) row still counted as WORK (calls≥1, spend_micros 0)",
            any(r["day"] == "2026-08-12" and r["spend_micros"] == 0 and r["calls"] >= 1 for r in rows))
fails += ck("day-less row EXCLUDED (no row carries an empty day)", all(r["day"] for r in rows))
fails += ck("project preserved for attribution", any(r["project"] == "manga2anime" for r in rows))
fails += ck("member_ref stamped on every row", all(r["member_ref"] == "ash@healiom.com" for r in rows))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_value_day_totals: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
