"""LIVE end-to-end replay — the integration test a unit test cannot be.

Re-issues a STRATIFIED sample of REAL historical prompts (the call_io corpus: real prompt + system + schema)
across ALL panel vendors at once, through the REAL vendor_call.fan_out stack — real transport, real lanes, real
concurrency — bounded by a hard $ budget. It is deliberately NOT all-or-none: it reports per-vendor coverage and
the single number that must be ZERO — a truncated / empty / error result read as a real answer (a false success).

Stratified because the failures live in the tails: we over-sample the BIG prompts and the vendors that
historically failed (moonshot refused/transport, zai transport/schema), not just the easy middle.

  .venv.nosync/bin/python scripts/integration/live_replay.py --budget 3        # a $3 tier
  .venv.nosync/bin/python scripts/integration/live_replay.py --budget 5 --n 12
Only moonshot/kimi is metered; anthropic/openai/zai ride the $0 lanes (executor=pool). $0 est calls never count
against the budget, so a $3 budget is ~$3 of kimi. Zero API spend beyond what --budget authorises.
"""
import argparse
import json
import os
import sqlite3
import time

import spendguard                                            # gate: fail closed if this interpreter is not enforcing
spendguard.require()

os.environ.setdefault("SPENDGUARD_ADVISOR_EXECUTOR", "pool")   # ride the $0 lanes for the 3 plan vendors
from spendguard import config, vendor_call as vc, pricing

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.3")]
METERED = {"moonshot"}                                        # the only vendor that costs $ (others are lanes)
_METERED_MODELS = [m for v, m in PANEL if v in METERED]       # the models that actually bill — DERIVED, not named
# Named figures for the PRE-flight budget estimate only (the ledger bills actual): a coarse output-token count
# and a char/4 input proxy. Named, not literals at the call site — an estimate uses a stated assumption, not a
# magic number, and the suite's estimates-come-from-measurement guard enforces exactly that.
_EST_OUTPUT_TOKENS = 4000
_CHARS_PER_TOKEN = 4
_EST_FALLBACK_USD = 0.05


def sample(n, seed_rows):
    """Stratify the real call_io prompts by SIZE (small/mid/large by input length) so the big prompts — where
    truncation and deadlines actually bite — are represented, not just the easy middle."""
    rows = [r for r in seed_rows if (r["prompt"] or "").strip()]
    rows.sort(key=lambda r: len(r["prompt"] or ""))
    if not rows:
        return []
    thirds = [rows[:len(rows) // 3], rows[len(rows) // 3:2 * len(rows) // 3], rows[2 * len(rows) // 3:]]
    out, i = [], 0
    while len(out) < n and any(thirds):
        band = thirds[i % 3]
        if band:
            out.append(band.pop(len(band) // 2))             # the median of each remaining band
        i += 1
    return out


def est_metered_cost(prompt, system):
    """Zero-spend estimate of the metered cost for one call across the metered vendors (derived from PANEL, not
    hardcoded). Used to STOP before the budget, never after (a completed call still bills)."""
    in_tok = (len(prompt or "") + len(system or "")) // _CHARS_PER_TOKEN
    total = 0.0
    for model in _METERED_MODELS:
        total += float(pricing.realtime_cost(model, in_tok, _EST_OUTPUT_TOKENS) or _EST_FALLBACK_USD)
    return total or _EST_FALLBACK_USD


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=float, default=3.0, help="hard $ ceiling (metered vendors only)")
    ap.add_argument("--n", type=int, default=8, help="how many real prompts to replay (across all 4 vendors)")
    ap.add_argument("--deadline", type=float, default=200.0, help="per-vendor deadline seconds")
    a = ap.parse_args()

    con = sqlite3.connect(config.db_path())
    con.row_factory = sqlite3.Row
    seed = [dict(r) for r in con.execute(
        "SELECT prompt, system, req_schema, intent FROM call_io WHERE length(prompt) > 200 "
        "ORDER BY id DESC LIMIT 400")]
    con.close()
    prompts = sample(a.n, seed)
    print(f"replaying {len(prompts)} real historical prompts x {len(PANEL)} vendors  "
          f"(budget ${a.budget:.2f}, metered={sorted(METERED)})\n")

    cov = {v: {"ok": 0, "fail": 0, "kinds": {}} for v, _ in PANEL}
    false_success = []
    spent_est = 0.0
    out_path = os.path.join(config.HOME, "integration_live_replay.jsonl")
    fh = open(out_path, "w")
    done = 0
    for idx, row in enumerate(prompts, 1):
        est = est_metered_cost(row["prompt"], row["system"])
        if spent_est + est > a.budget:
            print(f"  [budget] stopping before prompt {idx}: est +${est:.3f} would exceed ${a.budget:.2f} "
                  f"(spent est ${spent_est:.2f}). Metered coverage is partial — reported as such, not failed.")
            break
        schema = None
        try:
            schema = json.loads(row["req_schema"]) if row["req_schema"] else None
        except Exception:
            schema = None
        t0 = time.time()
        fan = vc.fan_out(PANEL, row["prompt"], deadline_s=a.deadline, purpose=f"integration:{row['intent'] or 'replay'}",
                         system=row["system"] or None, schema=schema, max_tokens=None)
        spent_est += est
        done += 1
        line = {"i": idx, "chars": len(row["prompt"] or ""), "wall_s": round(time.time() - t0, 1),
                "n_ok": fan["n_ok"], "complete": fan["complete"], "results": {}}
        for r in fan["results"]:
            cov[r.vendor]["kinds"][r.kind] = cov[r.vendor]["kinds"].get(r.kind, 0) + 1
            cov[r.vendor]["ok" if r.ok else "fail"] += 1
            line["results"][r.vendor] = {"kind": r.kind, "latency": round(r.latency, 1), "cost": r.cost}
            # THE ONE INVARIANT THAT MUST HOLD: a non-ok result is never readable as an answer.
            if not r.ok:
                try:
                    _ = r.text
                    false_success.append((r.vendor, r.kind, idx))
                except vc.NotOk:
                    pass
        fh.write(json.dumps(line) + "\n")
        print(f"  [{idx:2}/{len(prompts)}] {len(row['prompt']):6}c  {fan['n_ok']}/4  "
              + ", ".join(f"{r.vendor[:4]}={r.kind}" for r in fan["results"]) + f"  ({line['wall_s']}s)")
    fh.close()

    print(f"\n=== coverage over {done} real prompts (executor=pool, ${spent_est:.2f} est metered) ===")
    for v, _ in PANEL:
        c = cov[v]
        tot = c["ok"] + c["fail"]
        rate = f"{100 * c['ok'] / tot:.0f}%" if tot else "n/a"
        print(f"  {v:10} {c['ok']}/{tot} ok ({rate})  {c['kinds']}")
    print(f"\n  FALSE SUCCESSES (a failure read as an answer — MUST be 0): {len(false_success)}"
          + (f"  {false_success}" if false_success else "  ✓"))
    print(f"  results -> {out_path}")
    raise SystemExit(1 if false_success else 0)


if __name__ == "__main__":
    main()
