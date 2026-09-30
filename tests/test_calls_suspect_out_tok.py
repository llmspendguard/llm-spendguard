"""Data-integrity guard (Warden #1 part 3a): a per-call out_tok that EXCEEDS the model's real output ceiling is
physically impossible for ONE call — a backfill/aggregate row or a recording bug (measured 2026-09-25: 1M-68M out_tok
recorded under gpt-5-nano, ceiling 128K). calls.record_call FLAGS such a row (calls.suspect) so per-call analysis and
the estimate-vs-actual reconciliation can exclude it — while KEEPING the raw out_tok/cost (never a silent clamp or
delete). This is the prerequisite for a trustworthy ledger: you cannot reconcile from data you cannot trust."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-suspect-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_CALLS"] = "1"          # enable per-call recording (fails closed otherwise)
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


db = calls._calls_db()
cols = [c[1] for c in db.execute("PRAGMA table_info(calls)").fetchall()]
ck("calls has a `suspect` column", "suspect" in cols)


def _row(cid):
    return db.execute("SELECT out_tok, cost, suspect FROM calls WHERE id=?", (cid,)).fetchone()


print("-- a normal per-call out_tok (within ceiling) is NOT flagged --")
cid = calls.record_call("openai", "gpt-5-nano", "realtime", 0.001, in_tok=100, out_tok=5000, intent="t")
r = _row(cid)
ck("normal 5000 out_tok → suspect is NULL", r is not None and r[2] is None)

print("-- an IMPOSSIBLE per-call out_tok (> 2x ceiling) is flagged, raw value KEPT --")
cid2 = calls.record_call("openai", "gpt-5-nano", "realtime", 13.9, in_tok=1000, out_tok=1_008_000, intent="t")
r2 = _row(cid2)
ck("impossible 1,008,000 out_tok → suspect is set", r2 is not None and bool(r2[2]))
ck("the raw out_tok is PRESERVED (never clamped)", r2 is not None and r2[0] == 1_008_000)
ck("the raw cost is PRESERVED", r2 is not None and abs((r2[1] or 0) - 13.9) < 1e-6)
ck("the flag names the reason (ceiling)", r2 is not None and "ceiling" in (r2[2] or ""))

print("-- an out_tok AT the ceiling is fine; a MODERATE overrun (> 1.5x) is still caught --")
cid3 = calls.record_call("openai", "gpt-5-nano", "realtime", 0.05, in_tok=100, out_tok=128000, intent="t")
ck("128000 (== ceiling) → not flagged (within the 1.5x slop margin)", _row(cid3)[2] is None)
cid3b = calls.record_call("openai", "gpt-5-nano", "realtime", 0.4, in_tok=100, out_tok=200000, intent="t")
ck("200000 (1.56x the 128K ceiling — impossible for nano) → flagged (a 2x margin would miss it)",
   bool(_row(cid3b)[2]))

print("-- an unknown model (no curated ceiling) is NO LONGER a blind spot: the universal bound still catches absurdity --")
# BEFORE #2: no published ceiling → the check was SKIPPED, so a 5M / 68M out_tok on an uncurated model sailed through
# un-flagged (the deepseek/gpt-5-nano artifact class). NOW the resolver falls back output_ceiling → context_window →
# a hard universal bound, so absurdity is caught whatever the model — while a plausible count is still never flagged.
cid4 = calls.record_call("who", "who:mystery-model", "realtime", 0.5, in_tok=100, out_tok=5_000_000, intent="t")
ck("unknown-ceiling model + 5,000,000 out_tok → NOW flagged (universal bound, not blind)", bool(_row(cid4)[2]))
ck("...the flag names the universal basis", "universal" in (_row(cid4)[2] or ""))
cid4b = calls.record_call("who", "who:mystery-model", "realtime", 0.01, in_tok=100, out_tok=50_000, intent="t")
ck("unknown-ceiling model + a PLAUSIBLE 50,000 out_tok → NOT flagged (no false positive)", _row(cid4b)[2] is None)

print("-- the sanity-ceiling resolver is never blind: output_ceiling → context_window → universal --")
_c_oc, _b_oc = calls._out_tok_sanity_ceiling("openai:gpt-5-nano")
ck("a curated model resolves to its output_ceiling", _b_oc == "output_ceiling" and _c_oc > 0)
_c_u, _b_u = calls._out_tok_sanity_ceiling("who:mystery-model")
ck("an uncurated model falls back to the universal bound (never None)",
   _b_u == "universal_bound" and _c_u == calls._UNIVERSAL_OUT_TOK_SANITY)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_calls_suspect_out_tok: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
