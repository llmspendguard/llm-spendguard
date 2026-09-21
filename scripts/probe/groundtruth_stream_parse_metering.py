"""GROUND TRUTH (real spend, estimate-first): a REAL call through the newly-gated OpenAI spend surfaces produces a
ledger row with the ACTUAL token counts — proving the wrappers METER, not merely mark _spend_gated=True and record
$0 (the silent-leak trap the SDK-surface sweep cannot see). Exercises the two NEW capture SHAPES:
  (1) Responses `.stream()` — the event manager (_GatedEventStreamManager; ResponseStreamManager has no
      get_final_* accessor, so usage rides the events);
  (2) chat `.parse` — the RT structured-output helper (a separate self._post spend path beside `create`).
Estimate-first + --budget (API-spend protocol). Run UNDER the gate.

  ./.venv.nosync/bin/python scripts/probe/groundtruth_stream_parse_metering.py           # zero-spend estimate
  ./.venv.nosync/bin/python scripts/probe/groundtruth_stream_parse_metering.py --run      # ~<$0.001 real calls
"""
import argparse
import os
import sqlite3
import sys
import time

os.environ["SPENDGUARD_CALLS"] = "1"            # record to the calls ledger so the row can be read back
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard                               # noqa: E402
spendguard.require()                            # fail closed — refuse if this interpreter is not gated
from spendguard import config, pricing, gate    # noqa: E402

MODEL = "gpt-4o-mini"                            # cheap, NON-reasoning (a reasoning model burns the token budget on
#                                                  hidden reasoning and parse() raises LengthFinishReasonError);
#                                                  supports both the Responses API and structured-output parse.


def estimate():
    per_in, per_out = 24, 24                     # tiny bounded calls
    try:
        per = pricing.realtime_cost(MODEL, per_in, per_out) or 0.0
    except Exception:
        per = 0.0
    return {"model": MODEL, "calls": 2, "per": per, "total": round(per * 2, 6)}


def _max_rowid():
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT COALESCE(MAX(rowid),0) FROM calls").fetchone()[0]
    finally:
        con.close()


def _rows_since(rowid):
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT rowid, model, kind, COALESCE(in_tok,0), COALESCE(out_tok,0), "
                           "COALESCE(cost,0), executor FROM calls WHERE rowid>? ORDER BY rowid",
                           (rowid,)).fetchall()
    finally:
        con.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Real-call ground truth for the newly-gated stream/parse surfaces.")
    ap.add_argument("--run", action="store_true", help="actually spend (default: zero-spend estimate)")
    ap.add_argument("--budget", type=float, default=0.05, help="refuse if the estimate exceeds this ($)")
    a = ap.parse_args(argv)

    est = estimate()
    print(f"ground-truth: 2 real calls to {est['model']} (Responses.stream + chat.parse), estimate ~${est['total']:.6f}")
    if not a.run:
        print("  ESTIMATE ONLY — re-run with --run to execute under the gate.")
        return 0
    if est["total"] > a.budget:
        print(f"  🔴 REFUSED — estimate ${est['total']:.6f} exceeds --budget ${a.budget:.2f}.")
        return 2

    gate.install()                              # idempotent — ensure the SDK surfaces are wrapped in THIS process
    from spendguard import calls
    calls.set_context(intent="groundtruth:metering-check")   # tag the rows (avoids the untagged-spend warning)
    import openai
    from pydantic import BaseModel
    client = openai.OpenAI(api_key=config.api_key("OPENAI_API_KEY"))
    start = _max_rowid()

    # (1) Responses.stream — the EVENT manager path. Consume the stream; the gate captures usage from the events
    # (the ledger row below is the real verdict, not this script's own best-effort read).
    try:
        with client.responses.stream(model=MODEL, input="Reply with exactly one word: ok.",
                                     max_output_tokens=32) as s:
            for _event in s:
                pass
        print("  [1] Responses.stream — consumed OK")
    except Exception as e:
        print(f"  [1] Responses.stream — call error: {type(e).__name__}: {str(e)[:120]}")

    # (2) chat.completions.parse — the RT structured-output path (generous token room so a reply fits)
    class Ack(BaseModel):
        word: str
    try:
        comp = client.chat.completions.parse(model=MODEL, max_completion_tokens=256,
                                             messages=[{"role": "user", "content": "Return JSON with word set to ok."}],
                                             response_format=Ack)
        print(f"  [2] chat.parse — SDK usage in={comp.usage.prompt_tokens} out={comp.usage.completion_tokens}")
    except Exception as e:
        print(f"  [2] chat.parse — call error: {type(e).__name__}: {str(e)[:120]}")

    time.sleep(0.6)                              # let the last row commit
    rows = _rows_since(start)
    print(f"\n  ledger rows recorded since rowid {start}: {len(rows)}")
    for r in rows:
        print(f"    rowid={r[0]} model={r[1]} kind={r[2]} in={r[3]} out={r[4]} cost=${r[5]:.6f} exec={r[6]}")

    # VERDICT: each newly-gated surface produced a REALTIME row via the metered API, with REAL input tokens (>0) —
    # not a silent $0 / 0-token placeholder. (out_tok can be small; in_tok>0 proves the usage was captured.)
    rt = [r for r in rows if r[2] == "realtime" and r[6] == "api"]
    ok = len(rt) >= 2 and all(r[3] > 0 for r in rt)
    print("\n  VERDICT:", "🟢 PASS — each newly-gated surface wrote a ledger row with REAL tokens (metered, not $0)"
          if ok else "🔴 FAIL — a newly-gated surface did not produce a real-token ledger row (possible silent leak)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
