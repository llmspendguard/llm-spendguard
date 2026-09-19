"""LIVE repro (grounded against the REAL ledger, not a fixture): bulk_delegate(metered_only=True) now TAGS every
metered row with the caller's intent + executor='api' + a real caller — the fix for the wall-clock-daemon
attribution bug (commit 1767d09). Before the fix, the fanned metered_only votes recorded intent=None (→ '(none)'
+ a "PAID call with NO intent" warning) with caller=threading.py:run, because the metered call runs on a timeout
daemon whose thread-local context was empty.

Estimate-first (API-spend protocol): default prints the zero-spend estimate; `--run` makes 2 real metered calls
(~pennies) to config.advisor_model() via the SAME bulk_delegate(metered_only=True) fan honestreview's refute uses,
then reads the calls ledger to prove every row is tagged. Run UNDER the gate.
"""
import argparse
import os
import sqlite3
import sys
import time

os.environ["SPENDGUARD_CALLS"] = "1"        # ground against the ledger: recording must be on (it FAILS CLOSED otherwise)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard                            # noqa: E402
spendguard.require()                         # fail closed — refuse if this interpreter is not gated
from spendguard import config, pricing, lane_balance, calls, expected_output   # noqa: E402

INTENT = "honestreview:repro-METERED"
TASKS = ["Reply with exactly the one word: alpha", "Reply with exactly the one word: beta"]


def _model():
    return config.advisor_model()


def estimate():
    m = _model()
    # MEASUREMENT, never invented literals (test_estimates_come_from_measurement.py): per-call input is the
    # REAL task text; per-call output is the MEASURED/published expectation from expected_output.expect
    # (learned p90 for this sig → model history → the model's published ceiling), whose `basis` is reported.
    # A guessed constant here is how $34 got quoted for a $380 run — this path cannot make that mistake.
    per_in = max(1, max(len(t) for t in TASKS) // 4)          # real prompt chars → tokens (~4 chars/token)
    per_out, out_basis = expected_output.expect(m, sig=INTENT)
    per = 0.0
    try:
        per = pricing.realtime_cost(m, per_in, per_out) or 0.0
    except Exception:
        per = 0.0
    return {"model": m, "n": len(TASKS), "per": per, "total": round(per * len(TASKS), 5),
            "per_in": per_in, "per_out": per_out, "out_basis": out_basis}


def _max_rowid():
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT COALESCE(MAX(rowid),0) FROM calls").fetchone()[0]
    finally:
        con.close()


def _new_rows(after_rowid):
    """(rowid, intent, executor, caller, kind, cost) for THIS repro's intent recorded after the run started, plus a
    separate count of ANY untagged '(none)' rows that appeared — the exact failure signature."""
    con = sqlite3.connect(config.db_path())
    try:
        mine = con.execute("SELECT rowid,intent,executor,caller,kind,COALESCE(cost,0) FROM calls "
                           "WHERE rowid>? AND intent=? ORDER BY rowid", (after_rowid, INTENT)).fetchall()
        none_ct = con.execute("SELECT COUNT(*) FROM calls WHERE rowid>? AND (intent IS NULL OR intent='') "
                              "AND kind='realtime'", (after_rowid,)).fetchone()[0]
        return mine, none_ct
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live metered_only attribution repro (grounded against the ledger).")
    ap.add_argument("--run", action="store_true", help="actually spend (default: zero-spend estimate)")
    ap.add_argument("--budget", type=float, default=0.5, help="refuse if the estimate exceeds this ($)")
    a = ap.parse_args(argv)

    est = estimate()
    print(f"metered_only repro — {est['n']} tasks pinned to {est['model']} (metered_only=True), "
          f"estimate ~${est['total']:.5f} (per call ${est['per']:.5f}: ~{est['per_in']} in / {est['per_out']} out "
          f"tok, output basis={est['out_basis']})")
    if not a.run:
        print("  ESTIMATE ONLY — re-run with --run to execute under the gate.")
        return 0
    if est["total"] > a.budget:
        print(f"  🔴 REFUSED — estimate ${est['total']:.5f} exceeds --budget ${a.budget:.2f}.")
        return 2
    if not calls.enabled():
        print("  🔴 call logging is OFF — cannot ground against the ledger. Set SPENDGUARD_CALLS=1.")
        return 2

    start = _max_rowid()
    print(f"  running the fan (deadline_s=120 → the wall-clock daemon path)…  ledger at rowid {start}")
    res = lane_balance.bulk_delegate(TASKS, intent=INTENT, model_for=lambda _t: _model(),
                                     metered_only=True, return_keyed=True, deadline_s=120.0)
    time.sleep(0.5)                                          # let the last row commit
    served = sum(1 for v in (res.values() if isinstance(res, dict) else res) if isinstance(v, dict) and v.get("text"))
    mine, none_ct = _new_rows(start)

    print(f"\n  ledger rows tagged {INTENT} since rowid {start}: {len(mine)}   (fan served {served}/{len(TASKS)})")
    for r in mine:
        print(f"    rowid={r[0]} intent={r[1]!r} executor={r[2]!r} caller={r[3]!r} kind={r[4]} cost=${r[5]:.5f}")
    print(f"  untagged '(none)' realtime rows since rowid {start}: {none_ct}")

    # GROUND TRUTH, not a proxy: the bug left the daemon-run metered call with an EMPTY thread-local context, so its
    # row was recorded with intent=None (→ an untagged '(none)' row) and the daemon's OWN frame as caller. `mine` is
    # the rows carrying THIS repro's intent, so len(mine) >= n_tasks IS the fix — before it, the fan's rows landed in
    # none_ct instead and mine was empty. Both `mine` and none_ct are printed above; the per-row caller is there for
    # a human to read (whether a given frame string "is the timeout daemon" is a judgement, not a substring match, so
    # it is not turned into a machine pass/fail decider).
    ok = (len(mine) >= len(TASKS) and all(r[2] == "api" for r in mine))
    print("\n  VERDICT:", "🟢 PASS — the metered_only fan's rows carry the caller's intent (executor='api'); the fan's "
          "context reached the timeout daemon"
          if ok else "🔴 FAIL — the fan's rows are missing their intent (landed as untagged '(none)') or executor")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
