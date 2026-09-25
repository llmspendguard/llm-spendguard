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

from spendguard import calls, reconcile_calls  # noqa: E402

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
ck("dry-run sees the RECORDING_GAP cell", len(dry["cells"]) == 1)
ck("dry-run delta EXCLUDES the suspect row's $5 (3x$0.20=$0.60, not $5.60)", abs(dry["total_usd"] - 0.60) < 1e-6)
ck("dry-run wrote NOTHING to spend_events", _se_rows() == [])

print("-- apply: books the delta as basis=reconstructed, source=reconcile-calls --")
ap = reconcile_calls.reconcile(intent_like="describe", min_usd=0.05, apply=True)
rows = _se_rows()
ck("apply booked exactly one reconstructed row", len(rows) == 1)
ck("it is basis=reconstructed (NOT billed — local, not provider-verified)", rows and rows[0][0] == "reconstructed")
ck("its amount is the delta ($0.60), suspect excluded", rows and abs((rows[0][2] or 0) - 0.60) < 1e-6)
ck("source=reconcile-calls (reversible)", rows and rows[0][1] == "reconcile-calls")

print("-- idempotent: re-applying books NOTHING more --")
ap2 = reconcile_calls.reconcile(intent_like="describe", min_usd=0.05, apply=True)
ck("re-apply finds no remaining gap (the reconstructed row now counts as ledger_billed)", len(ap2["cells"]) == 0)
ck("still exactly one reconstructed row (no double-count)", len(_se_rows()) == 1)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_reconcile_calls: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
