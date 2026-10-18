"""Lane visibility & value — the three pieces that make a subscription lane VISIBLE and its plan value COUNTED:

  (A) a lane-served call records WHICH lane (executor) + the project on the ledger row — a stored fact, not a
      provider-guess;
  (B) lane_value prices every active lane from that ledger, including session-mined lanes;
  (C) the receipt NAMES the lane inline — the one-line Stop-hook widget, the footer, and the full two-axis table.

Offline: isolated SPENDGUARD_HOME, calls-logging forced on, pricing stubbed deterministic, no LLM, no network.
"""
import os
import sys
import json
import tempfile
import sqlite3
import contextlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanevis-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_CALLS"] = "1"                    # the rich call log is opt-in; the lane fact lives on its rows
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls, lane_value, receipt, pricing, config, lane_catalog        # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


def _row(cid):
    with contextlib.closing(sqlite3.connect(config.db_path())) as c:
        return c.execute("SELECT executor, project, kind, cost FROM calls WHERE id=?", (cid,)).fetchone()


fails = []

# ── (A) the ledger row records the lane + project ────────────────────────────────────────────────────
print("-- (A) a subscription call records WHICH lane (executor) + project on the ledger row --")
cid = calls.record_call("gemini", "gemini-3-flash", "subscription", 0.0, in_tok=120_000, out_tok=20_000,
                   executor="gemini", project="Demo-Repo")
r = _row(cid)
fails += ck("executor persisted (the lane that served it)", r and r[0] == "gemini")
fails += ck("project persisted, lowercased to match the money ledger", r and r[1] == "demo-repo")
fails += ck("$0 billed · kind=subscription (the flat-fee plan served it)", r and r[2] == "subscription" and r[3] == 0.0)

# ── (B) lane_value prices every routed lane from the ledger, separate from miner value ───────────────
print("\n-- (B) lane_value prices every active lane from the ledger without changing miner value --")
for ex, prov, model in (("zai-coding", "zai", "unpriceable-fixture-model"),
                        ("claude-code", "anthropic", "claude-3-5-sonnet"),
                        ("codex", "openai", "gpt-5.1-codex")):
    calls.record_call(prov, model, "subscription", 0.0, in_tok=120_000, out_tok=20_000, executor=ex, project="demo-repo")

fails += ck("refreshed/priced set equals the entire active lane registry",
            lane_value.ledger_valued_lanes() == set(lane_catalog.lanes()))

_orig_price = pricing.realtime_cost
receipt.stamp_est_value([{"day": receipt._windows()[0], "spend_micros": 10_000_000, "billed": False}],
                        source="claude-code")
receipt.stamp_est_value([{"day": receipt._windows()[0], "spend_micros": 20_000_000, "billed": False}],
                        source="codex")
miner_before = json.loads(receipt._cache_path().read_text())["est_value_by_source"]
footer_before = next(line for line in receipt.render_tally(receipt.tally()).splitlines()
                     if "est sub value" in line)
try:
    # deterministic $/token so the test is independent of the synced price cache (the value, not the rate, is the point)
    def fixture_price(model, in_tok, out_tok=0, **kw):
        if model == "unpriceable-fixture-model":
            raise KeyError(model)
        return in_tok * 1e-6 + out_tok * 2e-6
    pricing.realtime_cost = fixture_price
    stamped = lane_value.stamp_from_ledger()
finally:
    pricing.realtime_cost = _orig_price

cache = json.loads(receipt._cache_path().read_text())
fails += ck("stamp_from_ledger valued every lane with in-window activity",
            set(stamped) == {"gemini", "zai-coding", "claude-code", "codex"})
fails += ck("all priceable active lanes receive their exact ledger-priced value",
            stamped == {"gemini": 0.16, "zai-coding": None, "claude-code": 0.16, "codex": 0.16})
fails += ck("ledger refresh does not alter the miner-fed footer cache",
            cache.get("est_value_by_source") == miner_before)
footer_after = next(line for line in receipt.render_tally(receipt.tally()).splitlines()
                    if "est sub value" in line)
fails += ck("footer est-sub-value total is unchanged by ledger pricing", footer_after == footer_before)
table = "\n".join(receipt._two_axis_table(receipt.tally()))
by_lane_line = next(line for line in table.splitlines() if line.startswith("Plan value by lane"))
active_with_calls = set(receipt._lane_activity()) & lane_value.ledger_valued_lanes()
fails += ck("every active registry lane with in-window ledger calls appears in by-lane value",
            all(lane in by_lane_line for lane in active_with_calls))
fails += ck("full-ledger miner values override routed subsets; other lanes retain routed value",
            all(f"{lane} ${value:.2f}" in by_lane_line and f"{lane} $0.00" not in by_lane_line
                for lane, value in (("gemini", 0.16), ("claude-code", 10.0), ("codex", 20.0))))
fails += ck("activity on an unpriceable lane is explicit unknown", "zai-coding unknown" in by_lane_line)
print(next(line for line in table.splitlines() if line.startswith("Plan value by lane")))
print("footer before:", footer_before)
print("footer after: ", footer_after)

# ── (C) the receipt NAMES the lane inline ─────────────────────────────────────────────────────────────
print("\n-- (C) the receipt surfaces the lane inline: one-line widget, footer, and full table --")
t = receipt.tally()
fails += ck("tally() carries the GLOBAL lane activity (which plan served the work)", bool(t.get("lanes")))
line = receipt.render_line(t)
fails += ck("one-line Stop-hook widget names a lane and marks it $0", "lanes:" in line and "gemini" in line and "$0" in line)
foot = receipt.render_tally(t)
fails += ck("footer names the lanes serving the work", "lanes serving your work" in foot and "gemini" in foot)
tree = receipt.render_tree()
fails += ck("full receipt splits plan value BY LANE (the per-lane est split)", "by lane" in tree)
fails += ck("full receipt names which lanes served the work", "lanes serving spendguard's work" in tree and "zai-coding" in tree)

# a scoped (per-repo) tally must NOT carry the global lane line — it would repeat the global figure under each repo
tp = receipt.tally(project="demo-repo")
fails += ck("a per-repo tally does NOT carry the global lane activity", not tp.get("lanes"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_visibility: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
