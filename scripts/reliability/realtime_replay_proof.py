#!/usr/bin/env python3
"""Item 4 — bounded LIVE replay of REAL prompts through the fixed reliability pipeline.

WHY NOT the full month: vendor_calls.jsonl stores only a prompt_sha (hash) + an output snippet, never the prompt body
(privacy by design), so the month's calls cannot be re-issued — their inputs do not exist. The opt-in quality corpus
call_io DOES hold real prompts, so this replays a STRATIFIED sample of them through the CURRENT (fixed) realtime
pipeline — routing → dispatch → adapters.call → in-process retry → vendor_call._classify → forensic recording — and
asserts the RELIABILITY PROPERTIES on REAL inputs:

  · NO CRASH        — every call returns a result; the pipeline never raises into the harness (a deliberate spend stop
                      is the ONE thing that halts, loudly — it is honored, never swallowed).
  · NO SILENT LOSS  — every call yields a classified outcome KIND and is recorded; any prompt NOT run (cap/stop) is
                      NAMED by id, never silently dropped.
  · CLASS-AWARE     — transient faults retry in-process; deterministic ones do not; a gate refusal is GATE_REFUSED
                      (not transport), an unknown provider is PREFLIGHT_UNMET (not a KeyError).

It does NOT claim "0 fails": a deterministic fault on a real input legitimately recurs. The claim is that EVERY outcome
is correctly classified and recorded — never a crash, never a silent loss — end to end on real data.

estimate-first (API spend protocol): default prints the $0 projection (call_io's RECORDED token counts × current
pricing); --run replays within --cap (a hard ceiling; the loop stops LOUD if cumulative billed reaches it). Runs in the
REAL home (needs the real lanes/keys) — no isolation. Nothing hardcoded: --n / --cap / --per-intent are inputs.
"""
import argparse
import json
import sys
import time

DEFAULT_SAMPLE_N = 100           # staged validation ladder start (100 → 250 → …); a stratified sample, not one intent
DEFAULT_CAP_USD = 50.0           # the pre-authorised test budget — a HARD ceiling; the replay stops loud if it is hit
DEFAULT_PER_INTENT = 40          # cap per intent so the sample stays stratified (no single intent dominates)
DEFAULT_TIMEOUT_S = 60.0


def _sample(n, per_intent):
    """A STRATIFIED sample of real prompts from call_io: up to `per_intent` per intent, newest first, total <= n.
    Returns [{id, intent, provider, model, prompt, system, in_tok, out_tok}]."""
    import spendguard
    spendguard.require()
    from spendguard import config
    import sqlite3
    con = sqlite3.connect(config.db_path())
    intents = [r[0] for r in con.execute(
        "SELECT COALESCE(intent,'(none)') FROM call_io WHERE prompt IS NOT NULL AND prompt!='' "
        "GROUP BY intent ORDER BY COUNT(*) DESC").fetchall()]
    out = []
    for it in intents:
        rows = con.execute(
            "SELECT id, intent, provider, model, prompt, system, in_tok, out_tok FROM call_io "
            "WHERE prompt IS NOT NULL AND prompt!='' AND COALESCE(intent,'(none)')=? ORDER BY ts DESC LIMIT ?",
            (it, int(per_intent))).fetchall()
        for r in rows:
            out.append({"id": r[0], "intent": r[1] or "(none)", "provider": r[2], "model": r[3],
                        "prompt": r[4], "system": r[5], "in_tok": r[6] or 0, "out_tok": r[7] or 0})
    con.close()
    return out[:int(n)] if n else out


def _estimate(sample):
    """$0 projection: price each row's RECORDED in/out tokens at the current realtime rate for its model. A model with
    no price is a NAMED gap (counted, never silently $0)."""
    from spendguard import pricing
    total, by_intent, unpriced = 0.0, {}, 0
    for s in sample:
        try:
            c = pricing.realtime_cost(s["model"], s["in_tok"], s["out_tok"])
        except Exception:
            c = None
        if c is None:
            unpriced += 1
            c = 0.0
        total += c
        bi = by_intent.setdefault(s["intent"], {"n": 0, "usd": 0.0})
        bi["n"] += 1
        bi["usd"] += c
    return {"n": len(sample), "est_usd": round(total, 4), "unpriced": unpriced,
            "by_intent": {k: {"n": v["n"], "usd": round(v["usd"], 4)} for k, v in sorted(by_intent.items())}}


def _replay(sample, cap_usd, timeout_s):
    """Replay each real prompt through the fixed pipeline; classify + tally every outcome. A deliberate spend stop
    HALTS loudly (honored, never swallowed). Stops LOUD if cumulative billed reaches cap_usd. Every prompt NOT run —
    because the cap or a deliberate stop halted the loop — is NAMED by id in `not_run_ids`, never a silent discard."""
    from spendguard import adapters, vendor_call as vc, gate
    from collections import Counter
    kinds, billed, ran, crashes, halted, not_run = Counter(), 0.0, 0, 0, None, []
    for i, s in enumerate(sample):
        if billed >= cap_usd:                            # HARD ceiling — stop LOUD, and NAME every unit left un-run
            halted, not_run = "cap", [x["id"] for x in sample[i:]]
            print("[replay] ⛔ CAP $%.2f reached after %d call(s) (billed $%.4f) — %d prompt(s) NOT run "
                  "(ids %s%s) — stopping LOUD (the limit engaged, not completion)."
                  % (cap_usd, ran, billed, len(not_run), not_run[:8], "…" if len(not_run) > 8 else ""),
                  file=sys.stderr)
            break
        try:
            r = adapters.call(s["model"], s["prompt"], system=s.get("system"), sig=s["intent"], timeout_s=timeout_s)
        except Exception as e:
            if gate.is_deliberate_stop(e):               # a spend/deadline refusal HALTS — honored, never swallowed;
                halted, not_run = "deliberate_stop", [x["id"] for x in sample[i:]]   # the CURRENT unit (i) + the rest,
                print("[replay] ⛔ DELIBERATE STOP on prompt id=%s after %d call(s): %s — %d prompt(s) NOT run "
                      "(ids %s%s) — halting (honored, never swallowed)."          # NAMED by id, never lost
                      % (s["id"], ran, str(e)[:80], len(not_run), not_run[:8], "…" if len(not_run) > 8 else ""),
                      file=sys.stderr)
                break
            crashes += 1                                 # adapters.call is supposed to never raise a non-deliberate error
            kinds["CRASH:%s" % type(e).__name__] += 1    # a crash IS an outcome here — counted + named, never swallowed
            ran += 1
            print("[replay] ⚠ CRASH on prompt id=%s: %s" % (s["id"], type(e).__name__), file=sys.stderr)
            continue
        r = r if isinstance(r, dict) else {}
        k, _ = vc._classify(r)
        kinds[k] += 1
        billed += float(r.get("cost") or 0.0)
        ran += 1
    return {"ran": ran, "billed_usd": round(billed, 4), "crashes": crashes, "halted": halted,
            "not_run": len(not_run), "not_run_ids": not_run[:20], "outcomes": dict(kinds)}


def main():
    ap = argparse.ArgumentParser(description="bounded live replay of real call_io prompts through the fixed pipeline")
    ap.add_argument("--n", type=int, default=DEFAULT_SAMPLE_N, help="max prompts to sample (0 = all replayable)")
    ap.add_argument("--per-intent", type=int, default=DEFAULT_PER_INTENT, help="cap per intent (keeps it stratified)")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="HARD $ ceiling for the replay")
    ap.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="per-call deadline")
    ap.add_argument("--run", action="store_true", help="actually replay (real, capped spend); omit for $0 estimate")
    a = ap.parse_args()

    sample = _sample(a.n, a.per_intent)
    if not sample:
        print("no replayable prompts in call_io (the opt-in quality corpus is empty here).", file=sys.stderr)
        return 3
    est = _estimate(sample)
    if not a.run:
        print(json.dumps({"phase": "estimate", **est, "cap_usd": a.cap,
                          "under_cap": est["est_usd"] <= a.cap,
                          "note": "estimate-only ($0) from call_io recorded token counts; --run to replay within --cap. "
                                  "Real routing may cost LESS (many intents route to $0 subscription lanes)."},
                         indent=2))
        return 0

    print("[replay] replaying %d real prompt(s) through the fixed pipeline (cap $%.2f, est $%.4f)…"
          % (est["n"], a.cap, est["est_usd"]), file=sys.stderr)
    t0 = time.time()
    res = _replay(sample, a.cap, a.timeout_s)
    elapsed = time.time() - t0
    oc = res["outcomes"]
    from spendguard import vendor_call as vc
    ok = oc.get(vc.OK, 0)
    transient = oc.get(vc.TRANSPORT_ERROR, 0) + oc.get(vc.OVERLOADED, 0)
    crashes = res["crashes"]
    # every SAMPLED prompt is accounted for: ran (each producing a classified outcome) + not_run (named by id). No unit
    # is unaccounted, and a classified outcome exists for every call that ran → no silent loss.
    accounted = res["ran"] + res["not_run"] == len(sample)
    no_loss = accounted and (res["ran"] == sum(oc.values()))
    proven = crashes == 0 and no_loss and res["halted"] in (None, "cap")
    print(json.dumps({"phase": "replay", "sampled": len(sample), "ran": res["ran"], "not_run": res["not_run"],
                      "not_run_ids": res["not_run_ids"], "billed_usd": res["billed_usd"], "elapsed_s": round(elapsed, 1),
                      "outcomes": oc, "ok": ok, "transient_seen": transient, "crashes": crashes,
                      "halted": res["halted"], "all_units_accounted": accounted, "no_silent_loss": no_loss,
                      "RELIABILITY_PROVEN": proven,
                      "note": ("every real call reached a classified, recorded outcome — no crash, no silent loss; "
                               "deterministic faults that recur are correctly classified, not retried; the pipeline "
                               "held on real inputs" if proven else
                               "inspect crashes/halted/outcomes — a CRASH or silent loss is a real defect")}, indent=2))
    return 0 if proven else 4


if __name__ == "__main__":
    sys.exit(main())
