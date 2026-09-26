"""reconcile_calls (Warden #1 part 3b): fill the money ledger (spend_events) from the LOCAL calls telemetry for
RECORDING_GAP cells — real billed successes in `calls` that never landed in spend_events (a non-gated context). Every
safety property is guarded here because this WRITES the money-of-record:
  • dry-run writes NOTHING; apply books the per-cell DELTA as basis=reconstructed on the historical day;
  • idempotent — re-applying books nothing more (a stable dedup_key + the delta going to 0);
  • suspect rows (part 3a) are EXCLUDED from the reconstructed amount;
  • reversible — every row carries source='reconcile-calls'.
Offline: an isolated ledger; no network, no admin key, no provider call."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-reconcalls-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_CALLS"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import sqlite3  # noqa: E402

from spendguard import budget, calls, reconcile_calls  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


INTENT = "warden:describe"
# a real billed success on a cheap model (well within ceiling → not suspect), recorded ONLY in `calls` (the gap)
for _ in range(3):
    calls.record_call("openai", "gpt-5-nano", "realtime", 0.20, in_tok=1000, out_tok=4000, intent=INTENT, finish="stop")
# a SUSPECT row (impossible out_tok, part 3a) with a big cost — must be EXCLUDED from the reconstructed amount
calls.record_call("openai", "gpt-5-nano", "realtime", 5.00, in_tok=1000, out_tok=1_000_000, intent=INTENT, finish="stop")

# THE double-count guard: a cell whose spend is ALREADY represented by an `estimate` row (spent_dec counts
# estimates) is NOT a gap and must book NOTHING — comparing calls against non-estimate rows only (the bug this
# fixes) would have re-booked the whole $3.00 that the estimate already stands in for.
INTENT_COV = "warden:describe_estimate_covered"                       # contains "describe" -> in scope of the filter
calls.record_call("openai", "gpt-5-nano", "realtime", 3.00, in_tok=800, out_tok=3000, intent=INTENT_COV, finish="stop")
_covcon = sqlite3.connect(reconcile_calls._ledger_path())
_cov_day = _covcon.execute("SELECT date(ts) FROM calls WHERE intent=? LIMIT 1", (INTENT_COV,)).fetchone()[0]
_covcon.close()
budget._record_spend_event("openai", "gpt-5-nano", "realtime", 3.00, basis=budget.BASIS_ESTIMATE,
                           intent=INTENT_COV, occurred_at=f"{_cov_day}T12:00:00+00:00",
                           dedup_key=f"test-estimate:{INTENT_COV}:{_cov_day}")

# THE honestreview-finding guard: a plan-covered basis='reconstructed' row (what source='claude-code' rows are)
# must NOT mask a real metered gap — spent_dec EXCLUDES reconstructed, so the cell is genuinely under-reported and
# a re-run must be able to repair it. (Name has no "describe" -> tested under its own filter so the counts above hold.)
INTENT_RECON = "warden:recon_masked_gap"
calls.record_call("openai", "gpt-5-nano", "realtime", 4.00, in_tok=900, out_tok=4000, intent=INTENT_RECON, finish="stop")
_rcon = sqlite3.connect(reconcile_calls._ledger_path())
_rec_day = _rcon.execute("SELECT date(ts) FROM calls WHERE intent=? LIMIT 1", (INTENT_RECON,)).fetchone()[0]
_rcon.close()
budget._record_spend_event("openai", "gpt-5-nano", "realtime", 4.00, basis=budget.BASIS_RECONSTRUCTED,
                           intent=INTENT_RECON, occurred_at=f"{_rec_day}T12:00:00+00:00", source="claude-code",
                           dedup_key=f"test-reconstructed:{INTENT_RECON}:{_rec_day}")


def _se_rows():
    # Read the SAME ledger reconcile_calls writes/reads (no hardcoded path assumption). Before apply the
    # spend_events table does not exist yet (nothing has written the money ledger) — that is zero rows, not an error.
    con = sqlite3.connect(reconcile_calls._ledger_path())
    try:
        return con.execute("SELECT cost_basis, source, ROUND(realtime_usd,4), day FROM spend_events "
                           "WHERE intent=? AND source=?", (INTENT, "reconcile-calls")).fetchall()
    except sqlite3.OperationalError:
        return []          # no spend_events table yet == no reconciled rows
    finally:
        con.close()


print("-- dry-run: finds the gap, writes NOTHING --")
dry = reconcile_calls.reconcile(intent_like="describe", min_usd=0.05, apply=False)
_cov_flagged = [c for c in dry["cells"] if c["intent"] == INTENT_COV]
ck("dry-run does NOT flag the estimate-covered cell (no double-count)", _cov_flagged == [])
ck("dry-run sees exactly the ONE true RECORDING_GAP cell", len(dry["cells"]) == 1)
ck("dry-run delta EXCLUDES the suspect row's $5 (3x$0.20=$0.60, not $5.60)", abs(dry["total_usd"] - 0.60) < 1e-6)
ck("dry-run wrote NOTHING via reconcile-calls", _se_rows() == [])

print("-- apply: books the delta as basis=billed, source=reconcile-calls --")
ap = reconcile_calls.reconcile(intent_like="describe", min_usd=0.05, apply=True)
rows = _se_rows()
ck("apply booked exactly one billed row", len(rows) == 1)
ck("it is basis=billed (same KIND as its gate-recorded siblings; lifts spent_dec)", rows and rows[0][0] == "billed")
ck("its amount is the delta ($0.60), suspect excluded", rows and abs((rows[0][2] or 0) - 0.60) < 1e-6)
ck("source=reconcile-calls (reversible)", rows and rows[0][1] == "reconcile-calls")

print("-- idempotent: re-applying books NOTHING more (the billed fill is now counted by _COUNTABLE) --")
ap2 = reconcile_calls.reconcile(intent_like="describe", min_usd=0.05, apply=True)
ck("re-apply finds no remaining gap (the billed fill now counts as represented)", len(ap2["cells"]) == 0)
ck("still exactly one billed row (no double-count)", len(_se_rows()) == 1)

print("-- honestreview finding: a plan-covered reconstructed row does NOT mask a metered gap --")
recon = reconcile_calls.recording_gaps(intent_like="recon_masked", min_usd=0.05)
ck("reconstructed (plan-covered) row does NOT hide the $4 metered gap",
   len(recon) == 1 and abs(recon[0]["delta"] - 4.00) < 1e-6)
reconcile_calls.reconcile(intent_like="recon_masked", min_usd=0.05, apply=True)
ck("after a billed fill the gap is repaired (re-run finds nothing)",
   reconcile_calls.recording_gaps(intent_like="recon_masked", min_usd=0.05) == [])

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reconcile_calls: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
