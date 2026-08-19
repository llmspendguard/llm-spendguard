"""Lane visibility & value — the three pieces that make a subscription lane VISIBLE and its plan value COUNTED:

  (A) a lane-served call records WHICH lane (executor) + the project on the ledger row — a stored fact, not a
      provider-guess;
  (B) lane_value prices the lanes that have NO session-log miner (gemini, zai-coding) from that ledger and stamps
      their est-value, WITHOUT re-pricing the session-mined lanes (claude-code/codex) — no double-count;
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

from spendguard import calls, lane_value, receipt, pricing, config                     # noqa: E402


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
cid = calls.record("gemini", "gemini-3-flash", "subscription", 0.0, in_tok=120_000, out_tok=20_000,
                   executor="gemini", project="Demo-Repo")
r = _row(cid)
fails += ck("executor persisted (the lane that served it)", r and r[0] == "gemini")
fails += ck("project persisted, lowercased to match the money ledger", r and r[1] == "demo-repo")
fails += ck("$0 billed · kind=subscription (the flat-fee plan served it)", r and r[2] == "subscription" and r[3] == 0.0)

# ── (B) lane_value prices the miner-less lanes from the ledger, and only those ────────────────────────
print("\n-- (B) lane_value stamps est-value for lanes with NO session miner (gemini/zai), never the mined ones --")
# a session-mined lane (claude-code) ALSO in the ledger — it must NOT be re-priced here (its value is mined elsewhere)
for ex, prov, model in (("zai-coding", "zai", "glm-4.6"), ("claude-code", "anthropic", "claude-3-5-sonnet")):
    calls.record(prov, model, "subscription", 0.0, in_tok=120_000, out_tok=20_000, executor=ex, project="demo-repo")

fails += ck("ledger_valued_lanes = lanes with no session miner (derived, not hardcoded)",
            lane_value.ledger_valued_lanes() == {"gemini", "zai-coding"})

_orig_price = pricing.realtime_cost
try:
    # deterministic $/token so the test is independent of the synced price cache (the value, not the rate, is the point)
    pricing.realtime_cost = lambda model, in_tok, out_tok=0, **kw: in_tok * 1e-6 + out_tok * 2e-6
    stamped = lane_value.stamp_from_ledger()
finally:
    pricing.realtime_cost = _orig_price

cache = json.loads(receipt._cache_path().read_text()).get("est_value_by_source") or {}
fails += ck("stamp_from_ledger valued exactly gemini + zai-coding", set(stamped) == {"gemini", "zai-coding"})
fails += ck("gemini est-value stamped > 0 (priced from its ledger tokens)", (cache.get("gemini") or {}).get("month", 0) > 0)
fails += ck("zai-coding est-value stamped > 0", (cache.get("zai-coding") or {}).get("month", 0) > 0)
fails += ck("session-mined claude-code NOT stamped by lane_value (no double-count)", "claude-code" not in cache)

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
